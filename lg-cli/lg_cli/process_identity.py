"""macOS process identity from public libproc/sysctl, without process arguments.

ABI: macOS SDK sys/proc_info.h struct proc_bsdinfo, PROC_PIDTBSDINFO=3;
sys/param.h MAXCOMLEN=16; sys/proc.h SZOMB=5; libproc.h proc_pidinfo.
startTicks retains the existing owner wire field: on Darwin it is the exact
process start timeval converted to integer microseconds, not scheduler ticks.
"""
import ctypes
import errno
import uuid


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'pbi_flags', 'pbi_status', 'pbi_xstatus', 'pbi_pid', 'pbi_ppid',
        'pbi_uid', 'pbi_gid', 'pbi_ruid', 'pbi_rgid', 'pbi_svuid', 'pbi_svgid', 'rfu_1')]
    _fields_ += [('pbi_comm', ctypes.c_char * 16), ('pbi_name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        'pbi_nfiles', 'pbi_pgid', 'pbi_pjobc', 'e_tdev', 'e_tpgid')]
    _fields_ += [('pbi_nice', ctypes.c_int32), ('pbi_start_tvsec', ctypes.c_uint64),
                ('pbi_start_tvusec', ctypes.c_uint64)]


def _bsd_info(pid):
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    query = library.proc_pidinfo
    query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    query.restype = ctypes.c_int
    info = _ProcBSDInfo()
    ctypes.set_errno(0)
    size = query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        failure = ctypes.get_errno()
        if size <= 0 and failure == errno.ESRCH:
            raise ProcessLookupError(errno.ESRCH, 'Process no longer exists')
        raise ValueError('Cannot verify run process identity')
    return info


def _boot_id():
    system = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    query = system.sysctlbyname
    query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                      ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(128)
    length = ctypes.c_size_t(ctypes.sizeof(buffer))
    if query(b'kern.bootsessionuuid', buffer, ctypes.byref(length), None, 0) != 0:
        raise ValueError('Cannot verify run boot identity')
    if not 1 < length.value <= ctypes.sizeof(buffer):
        raise ValueError('Cannot verify run boot identity')
    try:
        return str(uuid.UUID(buffer.value.decode('ascii')))
    except (ValueError, UnicodeError):
        raise ValueError('Cannot verify run boot identity') from None


def darwin_process_owner(pid):
    if type(pid) is not int or not 1 <= pid <= 2147483647:
        raise ValueError('Invalid run process identity')
    try:
        info = _bsd_info(pid)
        if info.pbi_pid != pid:
            raise ValueError('Cannot verify run process identity')
        if info.pbi_status == 5:
            return None
        if info.pbi_start_tvsec == 0 or info.pbi_start_tvusec >= 1000000:
            raise ValueError('Cannot verify run process start time')
        return {'pid': pid, 'startTicks': str(info.pbi_start_tvsec * 1000000 + info.pbi_start_tvusec),
                'bootId': _boot_id()}
    except ProcessLookupError:
        return None
    except (OSError, AttributeError):
        raise ValueError('Cannot verify run process ownership') from None
