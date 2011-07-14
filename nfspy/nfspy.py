#!/usr/bin/env python

# NFS-Fuse implementation with auth-spoofing
# by Daniel Miller

import sys
import rpc
import fuse
from errno import *
from socket import gethostname
from time import time
from nfsclient import *
from mountclient import TCPMountClient,UDPMountClient
import os
from threading import Lock
from lrucache import LRU

fuse.fuse_python_api = (0, 2)

class NFSStat(fuse.Stat):
    def __init__(self):
        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 0
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0

class EvilNFSClient(NFSClient):
    def mkcred(self):
        self.cred = rpc.AUTH_UNIX, rpc.make_auth_unix(int(time()),
            gethostname(), self.fuid, self.fgid, [])
        return self.cred

    def Listdir(self, dir, tsize):
        list = []
        ra = (dir, 0, tsize)
        while 1:
            (status, rest) = self.Readdir(ra)
            if status <> NFS_OK:
                raise NFSError(status)
            entries, eof = rest
            last_cookie = None
            for fileid, name, cookie in entries:
                list.append((fileid, name))
                last_cookie = cookie
            if eof or last_cookie is None:
                break
            ra = (ra[0], last_cookie, ra[2])
        return list


class NFSNode(object):
    def __init__(self):
        pass

class NFSFuse(fuse.Fuse):
    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)
        self.fuse_args.add("ro", True)
        self.authlock = Lock()
        self.cachetimeout = 30 # seconds
        self.cache = 1024
        self.mcl = None
        self.handles = None

    def main(self):
        return fuse.Fuse.main(self)

    def fsinit(self):
        if hasattr(self,"server"):
            self.host, self.path = self.server.split(':',1);
        else:
            raise fuse.FuseError, "No server specified"

        if hasattr(self,"udpmount"):
            self.mcl = UDPMountClient(self.host)
        else:
            self.mcl = TCPMountClient(self.host)

        try:
            status, dirhandle = self.mcl.Mnt(self.path)
            if status <> NFS_OK:
                raise NFSError(status)
        except NFSError as e:
            no = e.errno()
            raise IOError(no, os.strerror(no), self.path)
        if hasattr(self,"hide"):
            self.mcl.Umnt(self.path)
        self.rootdh = dirhandle
        self.ncl = EvilNFSClient(self.host)
        self.ncl.fuid = self.ncl.fgid = 0
        status, fattr = self.ncl.Getattr(self.rootdh)
        if status <> NFS_OK:
            raise NFSError(status)
        self.rootattr = fattr
        self.ncl.fuid = self.rootattr[3]
        self.ncl.fgid = self.rootattr[4]

        status, rest = self.ncl.Statfs(self.rootdh)
        if status <> NFS_OK:
            raise NFSError(status)
        self.tsize = rest[0]
        if not self.tsize:
            self.tsize = 4096
        sys.stderr.write("cache = %d\ntimeout = %d" % (self.cache,self.cachetimeout))
        self.handles = LRU(self.cache)


    def _gethandle(self, dh, elem):
        status, rest = self.ncl.Lookup((dh, elem))
        if status <> NFS_OK:
            raise NFSError(status)
        else:
            dh, fattr = rest
            self.ncl.fuid = fattr[3]
            self.ncl.fgid = fattr[4]
        return (dh, fattr)

    def gethandle(self, path):
        elements = path.split("/")
        elements = filter(lambda x: x != '', elements)
        now = time()
        self.handles.prune(lambda x: now - x[2] > self.cachetimeout)
        dh = self.rootdh
        fattr = self.rootattr
        self.ncl.fuid = fattr[3]
        self.ncl.fgid = fattr[4]
        tmppath = ""
        for elem in elements:
            tmppath += "/" + elem
            try:
                dh, fattr, cachetime = self.handles[tmppath]
            except KeyError:
                dh, fattr = self._gethandle(dh, elem)
                self.handles[tmppath] = (dh, fattr, now)
            self.ncl.fuid = fattr[3]
            self.ncl.fgid = fattr[4]
        return (dh, fattr)

    #'getattr'
    def getattr(self, path):
        self.authlock.acquire()
        try:
            handle, fattr = self.gethandle(path)
            status, rest = self.ncl.Getattr(handle)
            if status <> NFS_OK:
                raise NFSError(status)
            else:
                fattr = rest
                self.handles[path] = (handle, fattr, time())
        except NFSError as e:
            no = e.errno()
            raise IOError(no, os.strerror(no), path)
        finally:
            self.authlock.release()
        st = NFSStat()
        st.st_mode, st.st_nlink, st.st_uid, st.st_gid, st.st_size \
            = fattr[1:6]
        st.st_atime = fattr[11][0]
        st.st_mtime = fattr[12][0]
        st.st_ctime = fattr[13][0]
        return st

    #'readlink'
    def readlink(self, path):
        if path == "/":
            return ''
        else:
            self.authlock.acquire()
            try:
                handle, fattr = self.gethandle(path)
            except NFSError as e:
                self.authlock.release()
                no = e.errno()
                raise IOError(no, os.strerror(no), path)
        try:
            status, rest = self.ncl.Readlink(handle)
            if status <> NFS_OK:
                raise NFSError(status)
            else:
                return rest
        except NFSError as e:
            no = e.errno()
            raise IOError(no, os.strerror(no), path)
        finally:
            self.authlock.release()

    #'readdir'
    def readdir(self, path, offset):
        self.authlock.acquire()
        if path == "/":
            handle, fattr = self.rootdh, self.rootattr
            self.ncl.fuid = self.rootattr[3]
            self.ncl.fgid = self.rootattr[4]
        else:
            try:
                handle, fattr = self.gethandle(path)
            except NFSError as e:
                self.authlock.release()
                no = e.errno()
                raise IOError(no, os.strerror(no), path)
        try:
            entries = (fuse.Direntry(dir[1]) for dir in self.ncl.Listdir(handle, self.tsize))
        except NFSError as e:
            no = e.errno()
            raise IOError(no, os.strerror(no), path)
        finally:
            self.authlock.release()
        return entries

    #'mknod'
    #'mkdir'
    #'unlink'
    #'rmdir'
    #'symlink'
    #'rename'
    #'link'
    #'chmod'
    #'chown'
    #'truncate'
    #'utime'
    #'open'
    #'read'
    def read(self, path, size, offset):
        if path == "/":
            raise IOError( EISDIR, os.strerror(EISDIR))
        else:
            self.authlock.acquire()
            try:
                handle, fattr = self.gethandle(path)
            except NFSError as e:
                self.authlock.release()
                no = e.errno()
                raise IOError(no, os.strerror(no), path)
        try:
            status, rest = self.ncl.Read((handle, offset, size, 0))
            if status <> NFS_OK:
                raise NFSError(status)
            else:
                fattr, data = rest
        except NFSError as e:
            no = e.errno()
            raise IOError(no, os.strerror(no), path)
        finally:
            self.authlock.release()
        return data

    #'write'
    #'release'
    #'statfs'
    def statfs(self):
        status, rest = self.ncl.Statfs(self.rootdh)
        if status <> NFS_OK:
            return -ENOSYS
        st = fuse.StatVfs()
        st.f_tsize, st.f_bsize, st.f_blocks, st.f_bfree, st.f_bavail = rest
        return st

    #'fsync'
    #'create'
    #'opendir'
    #'releasedir'
    #'fsyncdir'
    #'flush'
    #'fgetattr'
    #'ftruncate'
    #'getxattr'
    #'listxattr'
    #'setxattr'
    #'removexattr'
    #'access'
    def access(self, path, mode):
        self.authlock.acquire()
        try:
            handle, fattr = self.gethandle(path)
        except NFSError as e:
            no = e.errno()
            raise IOError(no, os.strerror(no), path)
        finally:
            self.authlock.release()
        if mode == os.F_OK:
            return 0
        rmode = fattr[1]
        uid = fattr[3]
        gid = fattr[4]
        if uid <> 0 and gid <> 0:
            return 0
        elif gid <> 0:
            if mode & os.R_OK and rmode & 044:
                return 0
            elif mode & os.W_OK and rmode & 022:
                return 0
            elif mode & os.X_OK and rmode & 011:
                return 0
            else:
                raise IOError(EACCES, os.strerror(EACCES), path)
        elif uid <> 0:
            if mode & os.R_OK and rmode & 0404:
                return 0
            elif mode & os.W_OK and rmode & 0202:
                return 0
            elif mode & os.X_OK and rmode & 0101:
                return 0
            else:
                raise IOError(EACCES, os.strerror(EACCES), path)
        else: #uid and gid == 0
            if mode & os.R_OK and rmode & 4:
                return 0
            elif mode & os.W_OK and rmode & 2:
                return 0
            elif mode & os.X_OK and rmode & 1:
                return 0
            else:
                raise IOError(EACCES, os.strerror(EACCES), path)

    #'lock'
    #'utimens'
    #'bmap'
    #'fsinit'
    #'fsdestroy'
    def fsdestroy(self):
        if not hasattr(self,"hide"):
            self.mcl.Umnt(self.path)


class NFSStatVfs(fuse.StatVfs):
    def __init__(self, **kw):
        self.f_tsize = 0
        fuse.StatVfs.__init__(self, **kw)

def main():
    usage="""
NFSFuse: An NFS client with auth spoofing. Must be run as root.

""" + fuse.Fuse.fusage

    server = NFSFuse(version="%prog " + fuse.__version__,
        usage=usage, dash_s_do='setsingle')
    server.parser.add_option(mountopt='server',metavar='HOST:PATH',
        help='connect to server HOST:PATH')
    server.parser.add_option(mountopt='hide',action='store_true',help='Immediately unmount from the server, staying mounted on the client')
    server.parser.add_option(mountopt='cache',type="int",default=100,help='Number of handles to cache')
    server.parser.add_option(mountopt='cachetimeout',type="int",default=30,help='Timeout on handle cache')
    server.parser.add_option(mountopt='udpmount',action='store_true',help='Use UDP transport for mount operation')
    server.parse(values=server, errex=1)
    server.main()

if __name__ == '__main__':
    main()
