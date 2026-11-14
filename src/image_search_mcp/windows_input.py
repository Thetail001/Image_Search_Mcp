"""Windows-only allowlist checks and no-reparse, rename-locked file opening."""
from __future__ import annotations

from contextlib import contextmanager
import msvcrt
import os
from pathlib import Path

import ntsecuritycon as rights
import pywintypes
import win32api
import win32con
import win32file
import win32security

from .local_input import LocalInputError

# A conservative DACL check: only this user, SYSTEM and administrators may write.
# Deny ACEs are not used to excuse a broad allow ACE (fail closed).
_WRITE_ACCESS = (rights.FILE_WRITE_DATA | rights.FILE_APPEND_DATA |
                 rights.FILE_WRITE_EA | rights.FILE_WRITE_ATTRIBUTES |
                 rights.FILE_DELETE_CHILD | rights.DELETE | rights.WRITE_DAC |
                 rights.WRITE_OWNER | rights.GENERIC_WRITE | rights.GENERIC_ALL |
                 rights.MAXIMUM_ALLOWED)
_ALLOWED_ACE_TYPES = {
    win32security.ACCESS_ALLOWED_ACE_TYPE,
    win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
    9, 11,  # ACCESS_ALLOWED_CALLBACK_ACE_TYPE / ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE
}


def _check_acl(handle) -> None:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    trusted = {win32security.ConvertSidToStringSid(user), 'S-1-5-18', 'S-1-5-32-544'}
    descriptor = win32security.GetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    dacl = descriptor.GetSecurityDescriptorDacl()
    if owner is None or win32security.ConvertSidToStringSid(owner) not in trusted or dacl is None:
        raise LocalInputError('LOCAL_INPUT_DISABLED', 'allowlist 目录需要可信所有者和受限 Windows ACL。')
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        ace_type, flags = ace[0]
        if flags & win32con.INHERIT_ONLY_ACE:
            continue
        if ace_type in _ALLOWED_ACE_TYPES and ace[1] & _WRITE_ACCESS:
            if win32security.ConvertSidToStringSid(ace[-1]) not in trusted:
                raise LocalInputError('LOCAL_INPUT_DISABLED', 'allowlist 目录不能对其他账户或组授予写权限。')


def _path_shape(path: Path) -> None:
    # Do not allow UNC/device namespaces, ADS, DOS device names or Win32 aliases.
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
    reserved.update(f'{prefix}{digit}' for prefix in ('COM', 'LPT') for digit in '123456789¹²³')
    if not path.is_absolute() or path.drive.startswith('\\'):
        raise LocalInputError('INPUT_PATH_INVALID', '仅支持本机驱动器上的绝对路径。')
    for part in path.parts[1:]:
        if (part in {'.', '..'} or ':' in part or part.endswith(('.', ' ')) or
                part.split('.')[0].upper() in reserved):
            raise LocalInputError('INPUT_PATH_INVALID', '路径含不受支持的 Windows 名称。')


@contextmanager
def _locked_chain(path: Path, *, directory: bool):
    """Pin every ancestor without FILE_SHARE_DELETE until the file is acquired.

    Each component is opened as the reparse point itself and inspected by handle.
    Ancestors cannot be renamed/deleted while pinned. The final file also denies
    concurrent writers; its resolved path is checked before any bytes are read.
    """
    _path_shape(path)
    handles = []
    current = Path(path.anchor)
    paths = [current]
    for part in path.parts[1:]:
        current = current / part
        paths.append(current)
    try:
        for index, component in enumerate(paths):
            is_directory = index < len(paths) - 1 or directory
            access = rights.READ_CONTROL | rights.FILE_READ_ATTRIBUTES
            if not is_directory:
                access |= rights.FILE_READ_DATA
            handle = win32file.CreateFile(
                str(component), access,
                win32con.FILE_SHARE_READ | (win32con.FILE_SHARE_WRITE if is_directory else 0),
                None, win32con.OPEN_EXISTING,
                win32file.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            handles.append(handle)
            attributes = win32file.GetFileInformationByHandle(handle)[0]
            if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise LocalInputError('INPUT_PATH_DENIED', '路径包含重解析点，已拒绝。')
            if bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY) != is_directory:
                raise LocalInputError('INPUT_FILE_UNAVAILABLE', '只接受普通图片文件及普通父目录。')
            if win32file.GetFileType(handle) != win32file.FILE_TYPE_DISK:
                raise LocalInputError('INPUT_FILE_UNAVAILABLE', '只接受磁盘上的普通文件。')
            final = win32file.GetFinalPathNameByHandle(handle, 0)
            if not final.startswith('\\\\?\\') or Path(final[4:]) != component:
                raise LocalInputError('INPUT_PATH_DENIED', '打开后的真实路径与允许路径不一致。')
        yield handles
    except pywintypes.error as exc:
        raise LocalInputError('INPUT_FILE_UNAVAILABLE', '图片文件或目录不可读，或正在被其他程序修改。') from exc
    finally:
        for handle in reversed(handles):
            handle.Close()


def validate_root(root: Path) -> None:
    try:
        with _locked_chain(root, directory=True) as handles:
            _check_acl(handles[-1])
    except (LocalInputError, pywintypes.error) as exc:
        raise LocalInputError('LOCAL_INPUT_DISABLED', 'allowlist 目录未通过 Windows 路径或权限检查。') from exc


def open_beneath(root: Path, candidate: Path) -> int:
    """Return a CRT fd owning the checked file handle; caller closes with os.close."""
    _path_shape(root)
    _path_shape(candidate)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LocalInputError('INPUT_PATH_DENIED', 'path 不在允许读取的图片目录内。') from exc
    with _locked_chain(candidate, directory=False) as handles:
        _check_acl(handles[len(root.parts) - 1])
        handle = handles.pop()
        raw = handle.Detach()
        try:
            return msvcrt.open_osfhandle(raw, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            win32api.CloseHandle(raw)
            raise
