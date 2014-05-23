import sys
import pyrax
import re
import swiftclient
import newrelic
from datetime import datetime

from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils.encoding import force_text

from cumulus.settings import CUMULUS
from cumulus.utils import (get_digest, gzip_content, read_gzipped_content,
                           get_content_type)


HEADER_PATTERNS = tuple((re.compile(p), h) for p, h in CUMULUS.get("HEADERS", {}))


def sync_headers(cloud_obj, headers={}, header_patterns=HEADER_PATTERNS):
    """
    Overwrites the given cloud_obj's headers with the ones given as ``headers`
    and adds additional headers as defined in the HEADERS setting depending on
    the cloud_obj's file name.
    """
    # don't set headers on directories
    content_type = getattr(cloud_obj, "content_type", None)
    if content_type == "application/directory":
        return
    matched_headers = {}
    for pattern, pattern_headers in header_patterns:
        if pattern.match(cloud_obj.name):
            matched_headers.update(pattern_headers.copy())
    # preserve headers already set
    matched_headers.update(cloud_obj.headers)
    # explicitly set headers overwrite matches and already set headers
    matched_headers.update(headers)
    if matched_headers != cloud_obj.headers:
        cloud_obj.headers = matched_headers
        cloud_obj.sync_metadata()


class SwiftclientStorage(Storage):
    """
    Custom storage for Swiftclient.
    """
    default_quick_listdir = True
    api_key = CUMULUS["API_KEY"]
    auth_url = CUMULUS["AUTH_URL"]
    region = CUMULUS["REGION"]
    connection_kwargs = {}
    container_name = CUMULUS["CONTAINER"]
    use_snet = CUMULUS["SERVICENET"]
    username = CUMULUS["USERNAME"]
    ttl = CUMULUS["TTL"]
    use_ssl = CUMULUS["USE_SSL"]
    use_pyrax = CUMULUS["USE_PYRAX"]

    def __init__(self, username=None, api_key=None, container=None,
                 connection_kwargs=None, container_uri=None):
        """
        Initializes the settings for the connection and container.
        """
        if username is not None:
            self.username = username
        if api_key is not None:
            self.api_key = api_key
        if container is not None:
            self.container_name = container
        if connection_kwargs is not None:
            self.connection_kwargs = connection_kwargs
        # connect
        if CUMULUS["USE_PYRAX"]:
            if CUMULUS["PYRAX_IDENTITY_TYPE"]:
                pyrax.set_setting("identity_type", CUMULUS["PYRAX_IDENTITY_TYPE"])
            pyrax.set_credentials(self.username, self.api_key, authenticate=False)

    def __getstate__(self):
        """
        Return a picklable representation of the storage.
        """
        return {
            "username": self.username,
            "api_key": self.api_key,
            "container_name": self.container_name,
            "use_snet": self.use_snet,
            "connection_kwargs": self.connection_kwargs
        }

    def _get_connection(self):
        if not hasattr(self, "_connection"):
            if CUMULUS["USE_PYRAX"]:
                public = not self.use_snet  # invert
                pyrax.set_credentials(self.username, self.api_key, authenticate=True)
                self._connection = pyrax.connect_to_cloudfiles(region=self.region,
                                                               public=public)
            else:
                self._connection = swiftclient.Connection(
                    authurl=CUMULUS["AUTH_URL"],
                    user=CUMULUS["USERNAME"],
                    key=CUMULUS["API_KEY"],
                    snet=CUMULUS["SERVICENET"],
                    auth_version=CUMULUS["AUTH_VERSION"],
                    tenant_name=CUMULUS["AUTH_TENANT_NAME"],
                )
        return self._connection

    def _set_connection(self, value):
        self._connection = value

    connection = property(_get_connection, _set_connection)

    def _get_container(self):
        """
        Gets or creates the container.
        """
        if not hasattr(self, "_container"):
            if CUMULUS["USE_PYRAX"]:
                self._container = self.connection.create_container(self.container_name)
            else:
                self._container = None
        return self._container

    def _set_container(self, container):
        """
        Sets the container (and, if needed, the configured TTL on it), making
        the container publicly available.
        """
        if CUMULUS["USE_PYRAX"]:
            if container.cdn_ttl != self.ttl or not container.cdn_enabled:
                container.make_public(ttl=self.ttl)
            if hasattr(self, "_container_public_uri"):
                delattr(self, "_container_public_uri")
        self._container = container

    container = property(_get_container, _set_container)

    def _get_container_url(self):
        if self.use_ssl and CUMULUS["CONTAINER_SSL_URI"]:
            self._container_public_uri = CUMULUS["CONTAINER_SSL_URI"]
        elif self.use_ssl:
            self._container_public_uri = self.container.cdn_ssl_uri
        elif CUMULUS["CONTAINER_URI"]:
            self._container_public_uri = CUMULUS["CONTAINER_URI"]
        else:
            self._container_public_uri = self.container.cdn_uri
        if CUMULUS["CNAMES"] and self._container_public_uri in CUMULUS["CNAMES"]:
            self._container_public_uri = CUMULUS["CNAMES"][self._container_public_uri]
        return self._container_public_uri

    container_url = property(_get_container_url)

    def _get_object(self, name):
        """
        Helper function to retrieve the requested Object.
        """
        try:
            return self.container.get_object(name)
        except pyrax.exceptions.NoSuchObject as err:
            pass


    def _open(self, name, mode="rb"):
        """
        Returns the SwiftclientStorageFile.
        """
        return SwiftclientStorageFile(storage=self, name=name)

    def _save(self, name, content):
        """
        Uses the Swiftclient service to write ``content`` to a remote
        file (called ``name``).
        """
        # Force the content type guess
        if hasattr(content.file, 'content_type'):
            del content.file.content_type
        content_type = get_content_type(content, name)
        try:
            content.file.content_type = content_type
        except AttributeError, e:
            # This may fail if the file object doesn't allow assignment
            if hasattr(newrelic, 'agent'): # only record this when comming from wsgi
                newrelic.agent.record_exception(*sys.exc_info())
        except:
            # Report if this fails
            if hasattr(newrelic, 'agent'): # only record this when comming from wsgi
                newrelic.agent.record_exception(*sys.exc_info())
        headers = {"Content-Type": content_type}

        # gzip the file if its of the right content type
        if content_type in CUMULUS.get("GZIP_CONTENT_TYPES", []):
            content_encoding = headers["Content-Encoding"] = "gzip"
        else:
            content_encoding = None

        if CUMULUS["USE_PYRAX"]:
            # TODO set headers
            if content_encoding == "gzip":
                content = gzip_content(content)
            data = content.read()
            self.connection.store_object(container=self.container_name,
                                         obj_name=name,
                                         data=data,
                                         content_type=content_type,
                                         content_encoding=content_encoding,
                                         etag=get_digest(data),
                                         return_none=True)
        else:
            # TODO gzipped content when using swift client
            data = content.read()
            self.connection.put_object(container=self.container_name,
                                       name=name,
                                       contents=data,
                                       etag=get_digest(data),
                                       content_type=content_type,
                                       headers=headers)

        return name

    def save(self, name, content):
        """
        Don't check for an available name before saving, just overwrite.
        """
        # Get the proper name for the file, as it will actually be saved.
        if name is None:
            name = content.name

        name = self._save(name, content)

        # Store filenames with forward slashes, even on Windows
        return force_text(name.replace('\\', '/'))

    def delete(self, name):
        """
        Deletes the specified file from the storage system.

        Deleting a model doesn't delete associated files: bit.ly/12s6Oox
        """
        try:
            self.connection.delete_object(self.container_name, name)
        except pyrax.exceptions.NoSuchObject as err:
            pass

    def exists(self, name):
        """
        Returns True if a file referenced by the given name already
        exists in the storage system, or False if the name is
        available for a new file.
        """
        return bool(self._get_object(name))

    def size(self, name):
        """
        Returns the total size, in bytes, of the file specified by name.
        """
        return self._get_object(name).total_bytes

    def modified_time(self, name):
        """
        Returns the last modified time (as datetime object) of the file
        specified by name.
        """
        mtime = self._get_object(name).last_modified
        if len(mtime) == 19:
            return datetime.strptime(mtime, '%Y-%m-%dT%H:%M:%S')
        return datetime.strptime(mtime, '%Y-%m-%dT%H:%M:%S.%f')

    def url(self, name):
        """
        Returns an absolute URL where the content of each file can be
        accessed directly by a web browser.
        """
        return "{0}/{1}".format(self.container_url, name)

    def listdir(self, path):
        """
        Lists the contents of the specified path, returning a 2-tuple;
        the first being an empty list of directories (not available
        for quick-listing), the second being a list of filenames.

        If the list of directories is required, use the full_listdir method.
        """
        files = []
        if path and not path.endswith("/"):
            path = "{0}/".format(path)
        path_len = len(path)
        for name in [x["name"] for x in
                     self.connection.get_container(self.container_name, full_listing=True)[1]]:
            files.append(name[path_len:])
        return ([], files)

    def full_listdir(self, path):
        """
        Lists the contents of the specified path, returning a 2-tuple
        of lists; the first item being directories, the second item
        being files.
        """
        dirs = set()
        files = []
        if path and not path.endswith("/"):
            path = "{0}/".format(path)
        path_len = len(path)
        for name in [x["name"] for x in
                     self.connection.get_container(self.container_name, full_listing=True)[1]]:
            name = name[path_len:]
            slash = name[1:-1].find("/") + 1
            if slash:
                dirs.add(name[:slash])
            elif name:
                files.append(name)
        dirs = list(dirs)
        dirs.sort()
        return (dirs, files)


class SwiftclientStorageFile(File):
    closed = False

    def __init__(self, storage, name, *args, **kwargs):
        self._storage = storage
        self._pos = 0
        self._chunks = None
        super(SwiftclientStorageFile, self).__init__(file=None, name=name,
                                                     *args, **kwargs)

    def _get_pos(self):
        return self._pos

    def _get_size(self):
        if not hasattr(self, "_size"):
            self._size = self._storage.size(self.name)
        return self._size

    def _set_size(self, size):
        self._size = size

    size = property(_get_size, _set_size)

    def _get_file(self):
        if not hasattr(self, "_file"):
            self._file = self._storage._get_object(self.name)
            self._file.tell = self._get_pos
        return self._file

    def _set_file(self, value):
        if value is None:
            if hasattr(self, "_file"):
                del self._file
        else:
            self._file = value

    file = property(_get_file, _set_file)

    def read(self, chunk_size=None):
        """
        Reads specified chunk_size or the whole file if chunk_size is None.

        If reading the whole file and the content-encoding is gzip, also
        gunzip the read content.

        If chunk_size is provided, the same chunk_size will be used in all
        further read() calls until the file is reopened or seek() is called.
        """
        if self._pos >= self._get_size() or chunk_size == 0:
            return ""

        if chunk_size is None and self._chunks is None:
            meta, data = self.file.get(include_meta=True)
            if meta.get('content-encoding', None) == 'gzip':
                data = read_gzipped_content(data)
        else:
            if self._chunks is None:
                # When reading by chunks, we're supposed to read the whole file
                # before calling get() again.
                self._chunks = self.file.get(chunk_size=chunk_size)

            try:
                data = self._chunks.next()
            except StopIteration:
                data = ""

        self._pos += len(data)
        return data

    def chunks(self, chunk_size=None):
        """
        Returns an iterator of file where each chunk has chunk_size.
        """
        if not chunk_size:
            chunk_size = self.DEFAULT_CHUNK_SIZE
        return self.file.get(chunk_size=chunk_size)

    def open(self, *args, **kwargs):
        """
        Opens the cloud file object.
        """
        self._pos = 0
        self._chunks = None

    def close(self, *args, **kwargs):
        self._pos = 0
        self._chunks = None

    @property
    def closed(self):
        return not hasattr(self, "_file")

    def seek(self, pos):
        self._pos = pos
        self._chunks = None


class StaticfilesMixin(object):
    """
    A mixin to automatically set the container to the one specified in
    CUMULUS["STATIC_CONTAINER"]. This provides the ability to specify a
    separate storage backend for Django's collectstatic command.

    To use, make sure CUMULUS["STATIC_CONTAINER"] is set to something other
    than CUMULUS["CONTAINER"]. Then, tell Django's staticfiles app by setting
    STATICFILES_STORAGE = "cumulus.storage.SwiftclientStaticStorage".
    """
    container_name = CUMULUS["STATIC_CONTAINER"]


class ThreadSafeMixin(object):
    """
    Extends SwiftclientStorage or a subclass to make it mostly thread safe.

    As long as you do not pass container or cloud objects between
    threads, you will be thread safe.

    Uses one connection/container per thread.
    """
    def __init__(self, *args, **kwargs):
        super(ThreadSafeMixin, self).__init__(*args, **kwargs)

        import threading
        self._local_cache = threading.local()

    def _get_connection(self):
        if not hasattr(self._local_cache, "connection"):
            public = not self.use_snet  # invert
            pyrax.set_credentials(self.username, self.api_key, authenticate=True)
            connection = pyrax.connect_to_cloudfiles(region=self.region,
                                                     public=public)
            self._local_cache.connection = connection

        return self._local_cache.connection

    connection = property(_get_connection, SwiftclientStorage._set_connection)

    def _get_container(self):
        if not hasattr(self._local_cache, "container"):
            container = self.connection.create_container(self.container_name)
            self._local_cache.container = container

        return self._local_cache.container

    container = property(_get_container, SwiftclientStorage._set_container)


class AttrDict(dict):
    """
    A dict object that allows you to access keys and values via attributes.
    """
    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self


class CachingMixin(object):
    """
    A mixin to add some threadlocal caching to the storage backend.
    """
    def __init__(self, *args, **kwargs):
        """
        If the ThreadSafeMixin is not used, emulate the threadlocal cache with
        an AttrDict instance.
        """
        super(CachingMixin, self).__init__(*args, **kwargs)
        if not hasattr(self, '_local_cache'):
            self._local_cache = AttrDict()

    def _get_obj_cache(self):
        """
        Retrieve the object metadata cache using the threadlocal if it's there,
        and hit the Cloud Files API for the container listing if the cache is
        empty.
        """
        if not hasattr(self._local_cache, 'objects'):
            self._local_cache.objects = {}
            for obj in self.container.get_objects(full_listing=True):
                self._local_cache.objects[obj.name] = obj
        return self._local_cache.objects

    def _set_obj_cache(self, objs):
        self._local_cache.objects = objs

    def _del_obj_cache(self):
        """
        Delete the cache so that it will be regenerated next time it is
        requested.
        """
        if hasattr(self._local_cache, 'objects'):
            del self._local_cache.objects

    _obj_cache = property(_get_obj_cache, _set_obj_cache, _del_obj_cache)

    def _get_object(self, name):
        """
        Use the object cache to retrieve the requested object.
        """
        if self.exists(name):
            return self._obj_cache[name]

    def exists(self, name):
        """
        Check for the object in the object cache.
        """
        return name in self._obj_cache

    """
    Invalidation
    """
    def _save(self, name, content):
        """
        Adjust the object cache to add the saved object.
        """
        available_name = super(CachingMixin, self)._save(name, content)
        self._obj_cache[available_name] = self.container.get_object(name)
        return available_name

    def delete(self, name):
        """
        Adjust the object cache to remove the deleted object.
        """
        if name in self._obj_cache:
            del self._obj_cache[name]
            return super(CachingMixin, self).delete(name)

    def _set_container(self, container, keep_cache=False):
        """
        A container switch invalidates the object cache.
        """
        if not keep_cache:
            del self._obj_cache
        return super(CachingMixin, self)._set_container(container)
