#!/usr/bin/env python3
'''
passthroughfs.py - Example file system for Python-LLFUSE

This file system mirrors the contents of a specified directory tree. It requires
Python 3.3 (since Python 2.x does not support the follow_symlinks parameters for
os.* functions).

Caveats:

 * Inode generation numbers are not passed through but set to zero.

 * Block size (st_blksize) and number of allocated blocks (st_blocks) are not
   passed through.

 * Performance for large directories is not good, because the directory
   is always read completely.

 * There may be a way to break-out of the directory tree.

 * The readdir implementation is not fully POSIX compliant. If a directory
   contains hardlinks and is modified during a readdir call, readdir()
   may return some of the hardlinked files twice or omit them completely.

 * If you delete or rename files in the underlying file system, the
   passthrough file system will get confused.

Copyright ©  Nikolaus Rath <Nikolaus.org>

This file is part of Python-LLFUSE. This work may be distributed under
the terms of the GNU LGPL.
'''

import os
import sys

# We are running from the Python-LLFUSE source directory, put it
# into the Python path.
basedir = os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]), '..'))
if (os.path.exists(os.path.join(basedir, 'setup.py')) and
    os.path.exists(os.path.join(basedir, 'src', 'llfuse'))):
    sys.path.append(os.path.join(basedir, 'src'))

import llfuse
from argparse import ArgumentParser
import errno
import logging
import stat as stat_m
from llfuse import FUSEError
from os import fsencode, fsdecode
from collections import defaultdict

log = logging.getLogger(__name__)

class Operations(llfuse.Operations):

    def __init__(self, source):
        super().__init__()
        self._inode_path_map = { llfuse.ROOT_INODE: source }
        self._lookup_cnt = defaultdict(lambda : 0)
        self._fd_inode_map = dict()
        self._inode_fd_map = dict()
        self._fd_open_count = dict()

    def _inode_to_path(self, inode):
        try:
            val = self._inode_path_map[inode]
        except KeyError:
            raise FUSEError(errno.ENOENT)

        if isinstance(val, set):
            # In case of hardlinks, pick any path
            val = next(iter(val))
        return val

    def _add_path(self, inode, path):
        log.debug('_add_path for %d, %s', inode, path)
        self._lookup_cnt[inode] += 1

        # With hardlinks, one inode may map to multiple paths.
        if inode not in self._inode_path_map:
            self._inode_path_map[inode] = path
            return

        val = self._inode_path_map[inode]
        if isinstance(val, set):
            val.add(path)
        elif val != path:
            self._inode_path_map[inode] = { path, val }

    def forget(self, inode_list):
        for (inode, nlookup) in inode_list:
            if self._lookup_cnt[inode] > nlookup:
                self._lookup_cnt[inode] -= nlookup
                continue
            log.debug('forgetting about inode %d', inode)
            assert inode not in self._inode_fd_map
            del self._lookup_cnt[inode]
            try:
                del self._inode_path_map[inode]
            except KeyError: # may have been deleted
                pass

    def lookup(self, inode_p, name):
        name = fsdecode(name)
        log.debug('lookup for %s in %d', name, inode_p)
        path = os.path.join(self._inode_to_path(inode_p), name)
        attr = self.getattr_path(path)
        if name != '.' and name != '..':
            self._add_path(attr.st_ino, path)
        return attr

    def getattr(self, inode):
        return self.getattr_path(self._inode_to_path(inode))

    def getattr_path(self, path):
        try:
            stat = os.lstat(path)
        except OSError as exc:
            raise FUSEError(exc.errno)

        entry = llfuse.EntryAttributes()
        for attr in ('st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                     'st_rdev', 'st_size', 'st_atime', 'st_mtime', 'st_ctime'):
            setattr(entry, attr, getattr(stat, attr))
        entry.generation = 0
        entry.entry_timeout = 5
        entry.attr_timeout = 5
        entry.st_blksize = 512
        entry.st_blocks = ((entry.st_size+entry.st_blksize-1) // entry.st_blksize)

        return entry

    def readlink(self, inode):
        path = self._inode_to_path(inode)
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        return fsencode(target)

    def opendir(self, inode):
        return inode

    def readdir(self, inode, off):
        path = self._inode_to_path(inode)
        log.debug('reading %s', path)
        entries = []
        for name in os.listdir(path):
            attr = self.getattr_path(os.path.join(path, name))
            entries.append((attr.st_ino, name, attr))

        log.debug('read %d entries, starting at %d', len(entries), off)

        # This is not fully posix compatible. If there are hardlinks
        # (two names with the same inode), we don't have a unique
        # offset to start in between them. Note that we cannot simply
        # count entries, because then we would skip over entries
        # (or return them more than once) if the number of directory
        # entries changes between two calls to readdir().
        for (ino, name, attr) in sorted(entries):
            if ino <= off:
                continue
            yield (fsencode(name), attr, ino)

    def unlink(self, inode_p, name):
        name = fsdecode(name)
        parent = self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            inode = os.lstat(path).st_ino
            os.unlink(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode in self._lookup_cnt:
            self._forget_path(inode, path)

    def rmdir(self, inode_p, name):
        name = fsdecode(name)
        parent = self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            inode = os.lstat(path).st_ino
            os.rmdir(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode in self._lookup_cnt:
            self._forget_path(inode, path)

    def _forget_path(self, inode, path):
        log.debug('forget %s for %d', path, inode)
        val = self._inode_path_map[inode]
        if isinstance(val, set):
            val.remove(path)
            if len(val) == 1:
                self._inode_path_map[inode] = next(iter(val))
        else:
            del self._inode_path_map[inode]

    def symlink(self, inode_p, name, target, ctx):
        name = fsdecode(name)
        target = fsdecode(target)
        parent = self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            os.symlink(target, path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        stat = os.lstat(path)
        self._add_path(stat.st_ino, path)
        return self.getattr(stat.st_ino)

    def rename(self, inode_p_old, name_old, inode_p_new, name_new):
        name_old = fsdecode(name_old)
        name_new = fsdecode(name_new)
        parent_old = self._inode_to_path(inode_p_old)
        parent_new = self._inode_to_path(inode_p_new)
        path_old = os.path.join(parent_old, name_old)
        path_new = os.path.join(parent_new, name_new)
        try:
            os.rename(path_old, path_new)
            inode = os.lstat(path_new).st_ino
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode not in self._lookup_cnt:
            return

        val = self._inode_path_map[inode]
        if isinstance(val, set):
            assert len(val) > 1
            set.add(path_new)
            set.remove(path_old)
        else:
            assert val == path_old
            self._inode_path_map[inode] = path_new

    def link(self, inode, new_inode_p, new_name):
        new_name = fsdecode(new_name)
        parent = self._inode_to_path(new_inode_p)
        path = os.path.join(parent, new_name)
        try:
            os.link(self._inode_to_path(inode), path, follow_symlinks=False)
        except OSError as exc:
            raise FUSEError(exc.errno)
        self._add_path(inode, path)
        return self.getattr(inode)

    def setattr(self, inode, attr):
        path = self._inode_to_path(inode)

        try:
            if attr.st_size is not None:
                os.truncate(path, attr.st_size)

            if attr.st_mode is not None:
                os.chmod(path, ~stat_m.S_IFMT & attr.st_mode,
                         follow_symlinks=False)


            assert (attr.st_uid is None) == (attr.st_gid is None)
            if attr.st_uid is not None:
                os.chown(path, attr.st_uid, attr.st_gid, follow_symlinks=False)

            assert (attr.st_atime is None) == (attr.st_mtime is None)
            if attr.st_atime is not None:
                os.utime(path, None, follow_symlinks=False,
                         ns=(attr.st_atime_ns, attr.st_mtime_ns))

        except OSError as exc:
            raise FUSEError(exc.errno)

        return self.getattr(inode)

    def mknod(self, inode_p, name, mode, rdev, ctx):
        path = os.path.join(self._inode_to_path(inode_p), fsdecode(name))
        try:
            os.mknod(path, mode=(mode & ~ctx.umask), device=rdev)
            os.chown(path, ctx.uid, ctx.gid)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = self.getattr_path(path)
        self._add_path(attr.st_ino, path)
        return attr

    def mkdir(self, inode_p, name, mode, ctx):
        path = os.path.join(self._inode_to_path(inode_p), fsdecode(name))
        try:
            os.mkdir(path, mode=(mode & ~ctx.umask))
            os.chown(path, ctx.uid, ctx.gid)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = self.getattr_path(path)
        self._add_path(attr.st_ino, path)
        return attr

    def statfs(self):
        stat_ = llfuse.StatvfsData()
        try:
            statfs = os.statvfs(self._inode_path_map[llfuse.ROOT_INODE])
        except OSError as exc:
            raise FUSEError(exc.errno)
        for attr in ('f_bsize', 'f_frsize', 'f_blocks', 'f_bfree', 'f_bavail',
                     'f_files', 'f_ffree', 'f_favail'):
            setattr(stat_, attr, getattr(statfs, attr))
        return stat_

    def open(self, inode, flags):
        if inode in self._inode_fd_map:
            fd = self._inode_fd_map[inode]
            self._fd_open_count[fd] += 1
            return fd
        assert flags & os.O_CREAT == 0
        try:
            fd = os.open(self._inode_to_path(inode), flags)
        except OSError as exc:
            raise FUSEError(exc.errno)
        self._inode_fd_map[inode] = fd
        self._fd_inode_map[fd] = inode
        self._fd_open_count[fd] = 1
        return fd

    def create(self, inode_p, name, mode, flags, ctx):
        path = os.path.join(self._inode_to_path(inode_p), fsdecode(name))
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_TRUNC)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = self.getattr_path(path)
        self._add_path(attr.st_ino, path)
        self._inode_fd_map[attr.st_ino] = fd
        self._fd_inode_map[fd] = attr.st_ino
        self._fd_open_count[fd] = 1
        return (fd, attr)

    def read(self, fd, offset, length):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)

    def write(self, fd, offset, buf):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, buf)

    def release(self, fd):
        if self._fd_open_count[fd] > 1:
            self._fd_open_count[fd] -= 1
            return

        del self._fd_open_count[fd]
        inode = self._fd_inode_map[fd]
        del self._inode_fd_map[inode]
        del self._fd_inode_map[fd]
        try:
            os.close(fd)
        except OSError as exc:
            raise FUSEError(exc.errno)

def init_logging(debug=False):
    formatter = logging.Formatter('%(asctime)s.%(msecs)03d %(threadName)s: '
                                  '[%(name)s] %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    if debug:
        handler.setLevel(logging.DEBUG)
        root_logger.setLevel(logging.DEBUG)
    else:
        handler.setLevel(logging.INFO)
        root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def parse_args(args):
    '''Parse command line'''

    parser = ArgumentParser()

    parser.add_argument('source', type=str,
                        help='Directory tree to mirror')
    parser.add_argument('mountpoint', type=str,
                        help='Where to mount the file system')
    parser.add_argument('--single', action='store_true', default=False,
                        help='Run single threaded')
    parser.add_argument('--debug', action='store_true', default=False,
                        help='Enable debugging output')

    return parser.parse_args(args)


def main():
    options = parse_args(sys.argv[1:])
    init_logging(options.debug)
    operations = Operations(options.source)

    log.debug('Mounting...')
    llfuse.init(operations, options.mountpoint,
                [  'fsname=passthroughfs', "nonempty",
                   'default_permissions' ])

    try:
        log.debug('Entering main loop..')
        llfuse.main(options.single)
    except:
        llfuse.close(unmount=False)
        raise

    log.debug('Unmounting..')
    llfuse.close()

if __name__ == '__main__':
    main()