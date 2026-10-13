// ntat: the file host's calls relative to a folder it holds open, on Windows (src/files/fd-paths-win32.ts).
//
// Windows has no srt, so on Windows this walk is what keeps the file tools inside the folder. Each name is opened
// relative to the handle of the folder before it, one component at a time, and never through a reparse point: a
// junction, a symlink, a mount point, an AppExecLink, a link the guest made, each is refused as Linux's O_NOFOLLOW
// refuses a link. Only the cloud files' placeholders (OneDrive's), which are files and folders, are taken. Every name
// is checked before it is opened, and the name that was opened is checked after: an 8.3 short name, a stream, a
// trailing dot or space, or a device's name never reaches what another name would.
//
// The walk is ported from Go's os.Root on Windows, as our own code: src/os/root_windows.go (rootOpenFileNolog,
// openat, mkdirat, removeat, renameat, linkat) and src/internal/syscall/windows/at_windows.go (Openat, Mkdirat,
// Deleteat, Renameat, Linkat), which open each component with NtCreateFile relative to its parent's handle, with
// FILE_OPEN_REPARSE_POINT and full sharing, and look at FILE_ATTRIBUTE_REPARSE_POINT on the new handle; their tests,
// src/os/root_test.go (TestRootOpenFileReparse, the junction and symlink cases of TestRootConsistency*) and
// src/internal/syscall/windows/at_windows_test.go, are what the tests of this addon mirror
// (test/ntat-win32.test.ts). Go's License is BSD.
//
// Each handle opened here becomes a descriptor of node.exe's own C runtime through libuv's uv_open_osfhandle, which
// node.exe exports, so Node's own fs calls that take a descriptor (read, write, fstat, ftruncate, fsync, close) work on
// what the walk opened, and Node's close closes it once. The handle under a descriptor is read back with
// uv_get_osfhandle. No handle is kept here.
//
// Loaded by the app's node on Windows, in the file host and its helper. N-API 8. Each call is synchronous, as Node's
// *Sync calls are. A failure throws an Error with the call's name as its syscall, libuv's own errno for the Win32 error
// (uv_translate_sys_error, which node.exe exports), and the NT status or Win32 error kept beside it; its loader
// (src/files/ntat.ts) makes it Node's form. Microsoft's documentation of each call is named at the call: Microsoft
// Learn, by the function or structure's page.

#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0A00
#include <windows.h>
#include <winternl.h>
#include <delayimp.h>
#include <sddl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define NAPI_VERSION 8
#include <node_api.h>

// libuv's own, as node.exe exports them (uv.h): a HANDLE as a descriptor of node's C runtime and back, and a Win32
// error as libuv's errno.
int uv_open_osfhandle(HANDLE os_fd);
HANDLE uv_get_osfhandle(int fd);
int uv_translate_sys_error(int sys_errno);

// node.exe's exports are reached from whatever program loads this addon: node.exe itself, or Electron's, which
// carries the same exports. node-gyp's win_delay_load_hook.cc does the same for every addon it builds.
static FARPROC WINAPI delay_hook(unsigned dliNotify, PDelayLoadInfo pdli) {
  if (dliNotify == dliNotePreLoadLibrary && _stricmp(pdli->szDll, "node.exe") == 0) return (FARPROC)GetModuleHandleW(NULL);
  return NULL;
}
const PfnDliHook __pfnDliNotifyHook2 = delay_hook;

// Each from ntdll's own headers' values (Microsoft Learn: NtCreateFile, OBJECT_ATTRIBUTES, FILE_INFORMATION_CLASS,
// FILE_RENAME_INFORMATION, FILE_DISPOSITION_INFORMATION_EX, FILE_LINK_INFORMATION), where winternl.h lacks them.
#ifndef NT_SUCCESS
#define NT_SUCCESS(status) (((NTSTATUS)(status)) >= 0)
#endif
#ifndef InitializeObjectAttributes
#define InitializeObjectAttributes(p, n, a, r, s) \
  do {                                            \
    (p)->Length = sizeof(OBJECT_ATTRIBUTES);      \
    (p)->RootDirectory = (r);                     \
    (p)->Attributes = (a);                        \
    (p)->ObjectName = (n);                        \
    (p)->SecurityDescriptor = (s);                \
    (p)->SecurityQualityOfService = NULL;         \
  } while (0)
#endif
#ifndef OBJ_CASE_INSENSITIVE
#define OBJ_CASE_INSENSITIVE 0x00000040L
#endif
// OBJECT_ATTRIBUTES: "If this flag is set, no reparse points will be followed when parsing the name of the associated
// object." Refused by build 10240 and taken from 19045 on (Go's issue 78131): Windows 11 takes it on every build.
#ifndef OBJ_DONT_REPARSE
#define OBJ_DONT_REPARSE 0x00001000L
#endif
#ifndef FILE_OPEN
#define FILE_OPEN 0x00000001
#endif
#ifndef FILE_CREATE
#define FILE_CREATE 0x00000002
#endif
#ifndef FILE_OPEN_IF
#define FILE_OPEN_IF 0x00000003
#endif
#ifndef FILE_OVERWRITE
#define FILE_OVERWRITE 0x00000004
#endif
#ifndef FILE_OVERWRITE_IF
#define FILE_OVERWRITE_IF 0x00000005
#endif
#ifndef FILE_DIRECTORY_FILE
#define FILE_DIRECTORY_FILE 0x00000001
#endif
#ifndef FILE_WRITE_THROUGH
#define FILE_WRITE_THROUGH 0x00000002
#endif
#ifndef FILE_SEQUENTIAL_ONLY
#define FILE_SEQUENTIAL_ONLY 0x00000004
#endif
#ifndef FILE_SYNCHRONOUS_IO_NONALERT
#define FILE_SYNCHRONOUS_IO_NONALERT 0x00000020
#endif
#ifndef FILE_NON_DIRECTORY_FILE
#define FILE_NON_DIRECTORY_FILE 0x00000040
#endif
#ifndef FILE_RANDOM_ACCESS
#define FILE_RANDOM_ACCESS 0x00000800
#endif
#ifndef FILE_OPEN_REPARSE_POINT
#define FILE_OPEN_REPARSE_POINT 0x00200000
#endif
// IO_STATUS_BLOCK's Information after a create: the file was made by this open.
#ifndef FILE_CREATED
#define FILE_CREATED 0x00000002
#endif
#define CLASS_ALTERNATE_NAME 21     // FileAlternateNameInformation: FILE_NAME_INFORMATION
#define CLASS_DISPOSITION 13        // FileDispositionInformation: FILE_DISPOSITION_INFORMATION
#define CLASS_DISPOSITION_EX 64     // FileDispositionInformationEx: FILE_DISPOSITION_INFORMATION_EX
#define CLASS_RENAME 10             // FileRenameInformation
#define CLASS_RENAME_EX 65          // FileRenameInformationEx
#define CLASS_LINK 11               // FileLinkInformation
#define CLASS_LINK_EX 72            // FileLinkInformationEx
#define DISPOSITION_DELETE 0x00000001
#define DISPOSITION_POSIX_SEMANTICS 0x00000002
#define DISPOSITION_IGNORE_READONLY_ATTRIBUTE 0x00000010
#define RENAME_REPLACE_IF_EXISTS 0x00000001
#define RENAME_POSIX_SEMANTICS 0x00000002
#define RENAME_IGNORE_READONLY_ATTRIBUTE 0x00000040
#define LINK_POSIX_SEMANTICS 0x00000002
// GetFileInformationByHandleEx's classes this addon reads (FILE_INFO_BY_HANDLE_CLASS), by number, as an older SDK
// may not name the last two.
#define BY_HANDLE_BASIC 0                 // FileBasicInfo
#define BY_HANDLE_STANDARD 1              // FileStandardInfo
#define BY_HANDLE_ATTRIBUTE_TAG 9         // FileAttributeTagInfo
#define BY_HANDLE_ID_BOTH_DIRECTORY 10    // FileIdBothDirectoryInfo
#define BY_HANDLE_ID_BOTH_RESTART 11      // FileIdBothDirectoryRestartInfo
#define BY_HANDLE_ID 18                   // FileIdInfo
#define BY_HANDLE_CASE_SENSITIVE 23       // FileCaseSensitiveInfo
#define CASE_SENSITIVE_DIR 0x00000001     // FILE_CS_FLAG_CASE_SENSITIVE_DIR
// RtlSetProcessPlaceholderCompatibilityMode's PHCM_EXPOSE_PLACEHOLDERS.
#define EXPOSE_PLACEHOLDERS 2
// MS-FSCC 2.1.2.1, Reparse Tags: IO_REPARSE_TAG_CLOUD and IO_REPARSE_TAG_CLOUD_1 to _F, which differ in bits 12-15.
#define CLOUD_TAG 0x9000001AUL
#define CLOUD_MASK 0xFFFF0FFFUL

// ntdll's calls, found by name in ntdll.dll at load, as Go's syscall package finds them: each is documented on
// Microsoft Learn under its Zw name or its own.
typedef NTSTATUS(NTAPI *CreateFileCall)(PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES, PIO_STATUS_BLOCK, PLARGE_INTEGER, ULONG, ULONG, ULONG,
                                       ULONG, PVOID, ULONG);
typedef NTSTATUS(NTAPI *InformationCall)(HANDLE, PIO_STATUS_BLOCK, PVOID, ULONG, ULONG);
typedef ULONG(NTAPI *DosErrorCall)(NTSTATUS);
typedef CHAR(NTAPI *PlaceholderCall)(CHAR);
static CreateFileCall nt_create_file;
static InformationCall nt_query_information_file;
static InformationCall nt_set_information_file;
static DosErrorCall rtl_nt_status_to_dos_error;
static PlaceholderCall rtl_set_placeholder_mode;

// FILE_RENAME_INFORMATION and FILE_LINK_INFORMATION, in their Ex form: Flags in place of the BOOLEAN ReplaceIfExists.
typedef struct {
  ULONG Flags;
  HANDLE RootDirectory;
  ULONG FileNameLength;
  WCHAR FileName[1];
} MoveInformation;

// One JavaScript call's arguments: at most eight.
#define MOST_ARGS 8
// The longest name one component may have, in UTF-16 units (NTFS's, and ReFS's).
#define NAME_UNITS 255
// The longest path Windows takes, in UTF-16 units, with its NUL.
#define PATH_UNITS 32768
// One listing's read, in bytes.
#define LISTING_BYTES 65536

// The one path buffer, for the calls that give a path: N-API calls run on the JavaScript thread alone.
static WCHAR path_buffer[PATH_UNITS];

// Why a call was refused before or after Windows was asked: a name Windows aliases, a reparse point on the way, or a
// name that opened as another.
enum Refused { NOT_REFUSED, REFUSED_NAME, REFUSED_LINK, REFUSED_ALIAS };
static const char *const REFUSED_WORDS[] = {NULL, "name", "link", "alias"};

static napi_value thrown(napi_env env, DWORD win32, const char *syscall, int ntstatus_set, NTSTATUS status, enum Refused refused) {
  napi_value message, error, number, name;
  napi_create_string_utf8(env, syscall, NAPI_AUTO_LENGTH, &message);
  napi_create_error(env, NULL, message, &error);
  napi_create_int32(env, uv_translate_sys_error((int)win32), &number);
  napi_set_named_property(env, error, "errno", number);
  napi_create_string_utf8(env, syscall, NAPI_AUTO_LENGTH, &name);
  napi_set_named_property(env, error, "syscall", name);
  napi_create_uint32(env, win32, &number);
  napi_set_named_property(env, error, "winerror", number);
  if (ntstatus_set) {
    napi_create_uint32(env, (uint32_t)status, &number);
    napi_set_named_property(env, error, "ntstatus", number);
  }
  if (refused != NOT_REFUSED) {
    napi_create_string_utf8(env, REFUSED_WORDS[refused], NAPI_AUTO_LENGTH, &name);
    napi_set_named_property(env, error, "refused", name);
  }
  napi_throw(env, error);
  return NULL;
}

static napi_value failed(napi_env env, const char *syscall) {
  return thrown(env, GetLastError(), syscall, 0, 0, NOT_REFUSED);
}

static napi_value failed_nt(napi_env env, NTSTATUS status, const char *syscall) {
  return thrown(env, rtl_nt_status_to_dos_error(status), syscall, 1, status, NOT_REFUSED);
}

// A name, a link or an alias refused: EINVAL for a name or an alias, which Windows would take for another; ELOOP for a
// reparse point, the answer Linux's O_NOFOLLOW gives (libuv's map takes ERROR_CANT_RESOLVE_FILENAME to ELOOP).
static napi_value refused(napi_env env, enum Refused why, const char *syscall) {
  return thrown(env, why == REFUSED_LINK ? ERROR_CANT_RESOLVE_FILENAME : ERROR_INVALID_PARAMETER, syscall, 0, 0, why);
}

static int args(napi_env env, napi_callback_info info, size_t want, napi_value *out) {
  size_t got = MOST_ARGS;
  if (napi_get_cb_info(env, info, &got, out, NULL, NULL) != napi_ok) return 0;
  if (got < want) {
    napi_throw_type_error(env, NULL, "ntat: too few arguments");
    return 0;
  }
  return 1;
}

static int number(napi_env env, napi_value value, uint32_t *out) {
  if (napi_get_value_uint32(env, value, out) != napi_ok) {
    napi_throw_type_error(env, NULL, "ntat: a descriptor, an access, a disposition, options and a share are numbers");
    return 0;
  }
  return 1;
}

static int truth(napi_env env, napi_value value, bool *out) {
  if (napi_get_value_bool(env, value, out) != napi_ok) {
    napi_throw_type_error(env, NULL, "ntat: a flag is true or false");
    return 0;
  }
  return 1;
}

// The handle under node's descriptor *value*; refused, with EBADF, where none is open.
static int handle_of(napi_env env, napi_value value, HANDLE *out, const char *syscall) {
  int32_t fd;
  if (napi_get_value_int32(env, value, &fd) != napi_ok) {
    napi_throw_type_error(env, NULL, "ntat: a descriptor is a number");
    return 0;
  }
  HANDLE handle = fd < 0 ? INVALID_HANDLE_VALUE : uv_get_osfhandle(fd);
  if (handle == INVALID_HANDLE_VALUE || handle == NULL) {
    thrown(env, ERROR_INVALID_HANDLE, syscall, 0, 0, NOT_REFUSED);
    return 0;
  }
  *out = handle;
  return 1;
}

static int ascii_upper(WCHAR c) {
  return c >= L'a' && c <= L'z' ? c - (L'a' - L'A') : c;
}

// Whether *stem*, *units* long, is one of Windows's device names, in any ASCII case: CON, PRN, AUX, NUL, CONIN$,
// CONOUT$, COM0-9 and LPT0-9, and COM and LPT with a superscript one, two or three (Microsoft Learn, "Naming Files,
// Paths, and Namespaces"; COM0 and LPT0 are refused too, which its list leaves out).
static int device(const WCHAR *stem, size_t units) {
  static const char *const plain[] = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"};
  for (size_t i = 0; i < sizeof plain / sizeof plain[0]; i++) {
    size_t length = strlen(plain[i]);
    if (length != units) continue;
    size_t at = 0;
    while (at < length && ascii_upper(stem[at]) == (WCHAR)plain[i][at]) at++;
    if (at == length) return 1;
  }
  if (units != 4) return 0;
  int com = ascii_upper(stem[0]) == L'C' && ascii_upper(stem[1]) == L'O' && ascii_upper(stem[2]) == L'M';
  int lpt = ascii_upper(stem[0]) == L'L' && ascii_upper(stem[1]) == L'P' && ascii_upper(stem[2]) == L'T';
  WCHAR last = stem[3];
  return (com || lpt) && ((last >= L'0' && last <= L'9') || last == 0x00B9 || last == 0x00B2 || last == 0x00B3);
}

// Whether *name*, *units* long, is one name Windows opens as itself (Microsoft Learn, "Naming Files, Paths, and
// Namespaces", and "File Streams"; Project Zero, "The Definitive Guide on Win32 to NT Path Conversion"): one or more
// units, not "." or ".."; none of \ / : * ? " < > | or 1-31; no trailing dot or space; no '~' and a digit, the form
// of every 8.3 short name NTFS makes; and not a device's name, alone or with any extension, with spaces before it.
// src/files/names-win32.ts says the same in the file tools' words, and a test holds the two together.
static int name_taken(const WCHAR *name, size_t units) {
  if (units == 0 || units > NAME_UNITS) return 0;
  if ((units == 1 && name[0] == L'.') || (units == 2 && name[0] == L'.' && name[1] == L'.')) return 0;
  size_t stem = units;
  for (size_t i = 0; i < units; i++) {
    WCHAR c = name[i];
    if (c < 32 || wcschr(L"\\/:*?\"<>|", c) != NULL) return 0;
    if (c == L'~' && i + 1 < units && name[i + 1] >= L'0' && name[i + 1] <= L'9') return 0;
    if (c == L'.' && stem == units) stem = i;
  }
  if (name[units - 1] == L'.' || name[units - 1] == L' ') return 0;
  while (stem > 0 && name[stem - 1] == L' ') stem--;
  return !device(name, stem);
}

// A name from JavaScript, as UTF-16, into *out* (NAME_UNITS + 1 units), checked: refused before anything is opened.
static int named(napi_env env, napi_value value, WCHAR *out, size_t *units, const char *syscall) {
  size_t length = 0;
  // Its whole length first: a longer name would be cut to fit, and the cut taken for it.
  if (napi_get_value_string_utf16(env, value, NULL, 0, &length) != napi_ok) {
    napi_throw_type_error(env, NULL, "ntat: a name is a string");
    return 0;
  }
  if (length > NAME_UNITS) {
    refused(env, REFUSED_NAME, syscall);
    return 0;
  }
  if (napi_get_value_string_utf16(env, value, (char16_t *)out, NAME_UNITS + 1, &length) != napi_ok || !name_taken(out, length)) {
    refused(env, REFUSED_NAME, syscall);
    return 0;
  }
  *units = length;
  return 1;
}

static int cloud(DWORD tag) {
  return (tag & CLOUD_MASK) == CLOUD_TAG;
}

// The last component of the name *handle* is open by, as Windows normalizes it, "all short names expanded"
// (GetFinalPathNameByHandleW, FILE_NAME_NORMALIZED with VOLUME_NAME_NONE: the path from the volume's root), in
// path_buffer from *at*, *units* long; 0 where it cannot be told.
static int long_name(HANDLE handle, size_t *at, size_t *units) {
  DWORD length = GetFinalPathNameByHandleW(handle, path_buffer, PATH_UNITS, FILE_NAME_NORMALIZED | VOLUME_NAME_NONE);
  if (length == 0 || length >= PATH_UNITS) return 0;
  size_t start = length;
  while (start > 0 && path_buffer[start - 1] != L'\\') start--;
  *at = start;
  *units = length - start;
  return 1;
}

// Whether *handle*, opened as *name*, is that name and nothing else: its long name is the name asked for, but for ASCII
// case (NTFS compares names by its volume's $UpCase table; a name in another case than the folder's is the same file).
// A short name, or any other alias, opens a file whose long name is another.
static int same_name(HANDLE handle, const WCHAR *name, size_t units) {
  size_t at, length;
  if (!long_name(handle, &at, &length) || length != units) return 0;
  for (size_t i = 0; i < units; i++) {
    if (ascii_upper(path_buffer[at + i]) != ascii_upper(name[i])) return 0;
  }
  return 1;
}

// What a handle is: a reparse point the walk does not go through, or not. A placeholder of the cloud files is a file or
// a folder (FileAttributeTagInfo: FILE_ATTRIBUTE_TAG_INFO, its ReparseTag where FILE_ATTRIBUTE_REPARSE_POINT is set).
static int link_at(HANDLE handle, int *link) {
  FILE_ATTRIBUTE_TAG_INFO tag;
  if (!GetFileInformationByHandleEx(handle, (FILE_INFO_BY_HANDLE_CLASS)BY_HANDLE_ATTRIBUTE_TAG, &tag, sizeof tag)) return 0;
  *link = (tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0 && !cloud(tag.ReparseTag);
  return 1;
}

// The options a caller may name: a folder or a file, and how it is read. Never FILE_OPEN_BY_FILE_ID, which would take a
// name for a file's number, nor FILE_DELETE_ON_CLOSE.
#define OPTIONS_TAKEN (FILE_DIRECTORY_FILE | FILE_NON_DIRECTORY_FILE | FILE_WRITE_THROUGH | FILE_SEQUENTIAL_ONLY | FILE_RANDOM_ACCESS)

// One name in the folder *dir*, opened by NtCreateFile relative to it (Microsoft Learn: NtCreateFile; OBJECT_ATTRIBUTES,
// RootDirectory: "ObjectName specifies an object name relative to the RootDirectory directory"), as Go's
// windows.Openat does: never following a reparse point (FILE_OPEN_REPARSE_POINT, OBJ_DONT_REPARSE), synchronous
// (FILE_SYNCHRONOUS_IO_NONALERT, with SYNCHRONIZE), and readable for its attributes. A reparse point that is no
// placeholder is refused, unless *links* asks for the link itself, as lstat does; and so is a name that opened as
// another. *units* 0 opens *dir* itself again, as ReOpenFile does: a handle of its own, with its own place in a listing.
static int open_at(napi_env env, HANDLE dir, const WCHAR *name, size_t units, ACCESS_MASK access, ULONG disposition, ULONG options,
                   ULONG share, ULONG attributes, bool links, HANDLE *out, const char *syscall) {
  UNICODE_STRING object;
  object.Length = object.MaximumLength = (USHORT)(units * sizeof(WCHAR));
  object.Buffer = (PWSTR)name;
  OBJECT_ATTRIBUTES attrs;
  InitializeObjectAttributes(&attrs, &object, OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE, dir, NULL);
  IO_STATUS_BLOCK io;
  HANDLE handle = NULL;
  NTSTATUS status = nt_create_file(&handle, access | SYNCHRONIZE | FILE_READ_ATTRIBUTES, &attrs, &io, NULL, attributes, share, disposition,
                                   (options & OPTIONS_TAKEN) | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT, NULL, 0);
  if (!NT_SUCCESS(status)) {
    failed_nt(env, status, syscall);
    return 0;
  }
  int link = 0;
  if (!link_at(handle, &link)) {
    DWORD error = GetLastError();
    CloseHandle(handle);
    thrown(env, error, syscall, 0, 0, NOT_REFUSED);
    return 0;
  }
  if (link && !links) {
    CloseHandle(handle);
    refused(env, REFUSED_LINK, syscall);
    return 0;
  }
  // A file this open made has the name it was made by; any other was found by a name, which must be its own.
  if (units > 0 && io.Information != FILE_CREATED && !same_name(handle, name, units)) {
    CloseHandle(handle);
    refused(env, REFUSED_ALIAS, syscall);
    return 0;
  }
  *out = handle;
  return 1;
}

// *handle* as node's descriptor, which owns it from then on.
static napi_value descriptor(napi_env env, HANDLE handle, const char *syscall) {
  int fd = uv_open_osfhandle(handle);
  if (fd < 0) {
    CloseHandle(handle);
    return thrown(env, ERROR_TOO_MANY_OPEN_FILES, syscall, 0, 0, NOT_REFUSED);
  }
  napi_value result;
  napi_create_int32(env, fd, &result);
  return result;
}

// openRoot(path): the folder a session is bound to, by its whole path (\\?\C:\...), as CreateFileW opens it
// (FILE_FLAG_BACKUP_SEMANTICS for a folder, FILE_FLAG_OPEN_REPARSE_POINT so that the folder itself is never a link
// followed), refused when it is a reparse point that is no placeholder or is not a folder. The first open asks that
// OneDrive's placeholders show as what they are (RtlSetProcessPlaceholderCompatibilityMode, PHCM_EXPOSE_PLACEHOLDERS):
// Microsoft's two pages on whether they are hidden by default disagree.
static napi_value open_root(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 1, argv)) return NULL;
  size_t length = 0;
  if (napi_get_value_string_utf16(env, argv[0], NULL, 0, &length) != napi_ok) {
    napi_throw_type_error(env, NULL, "ntat: a path is a string");
    return NULL;
  }
  if (length + 1 >= PATH_UNITS) return refused(env, REFUSED_NAME, "openRoot");
  napi_get_value_string_utf16(env, argv[0], (char16_t *)path_buffer, PATH_UNITS, &length);
  if (wcslen(path_buffer) != length) return refused(env, REFUSED_NAME, "openRoot");
  static int exposed = 0;
  if (!exposed && rtl_set_placeholder_mode != NULL) {
    rtl_set_placeholder_mode(EXPOSE_PLACEHOLDERS);
    exposed = 1;
  }
  HANDLE handle = CreateFileW(path_buffer, FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
                              FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
  if (handle == INVALID_HANDLE_VALUE) return failed(env, "openRoot");
  int link = 0;
  FILE_STANDARD_INFO standard;
  if (!link_at(handle, &link) || !GetFileInformationByHandleEx(handle, (FILE_INFO_BY_HANDLE_CLASS)BY_HANDLE_STANDARD, &standard, sizeof standard)) {
    DWORD error = GetLastError();
    CloseHandle(handle);
    return thrown(env, error, "openRoot", 0, 0, NOT_REFUSED);
  }
  if (link) {
    CloseHandle(handle);
    return refused(env, REFUSED_LINK, "openRoot");
  }
  if (!standard.Directory) {
    CloseHandle(handle);
    return thrown(env, ERROR_DIRECTORY, "openRoot", 0, 0, NOT_REFUSED);
  }
  return descriptor(env, handle, "openRoot");
}

// openAt(dir, name, access, disposition, options, share, attributes, links): one name in the folder open as *dir*,
// node's descriptor for it.
static napi_value open_at_call(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 8, argv)) return NULL;
  HANDLE dir;
  WCHAR name[NAME_UNITS + 1];
  size_t units;
  uint32_t access, disposition, options, share, attributes;
  bool links;
  if (!handle_of(env, argv[0], &dir, "openat") || !named(env, argv[1], name, &units, "openat") || !number(env, argv[2], &access)
      || !number(env, argv[3], &disposition) || !number(env, argv[4], &options) || !number(env, argv[5], &share)
      || !number(env, argv[6], &attributes) || !truth(env, argv[7], &links)) {
    return NULL;
  }
  if (disposition < FILE_OPEN || disposition > FILE_OVERWRITE_IF) return refused(env, REFUSED_NAME, "openat");
  HANDLE handle;
  if (!open_at(env, dir, name, units, access, disposition, options, share, attributes, links, &handle, "openat")) return NULL;
  return descriptor(env, handle, "openat");
}

// reopen(dir, access, share): the folder open as *dir* again, as a handle of its own: a listing's, or one held with
// another sharing than the walk's.
static napi_value reopen(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 3, argv)) return NULL;
  HANDLE dir, handle;
  uint32_t access, share;
  if (!handle_of(env, argv[0], &dir, "reopen") || !number(env, argv[1], &access) || !number(env, argv[2], &share)) return NULL;
  if (!open_at(env, dir, L"", 0, access, FILE_OPEN, 0, share, 0, false, &handle, "reopen")) return NULL;
  return descriptor(env, handle, "reopen");
}

// mkdirAt(dir, name): a folder made in the folder open as *dir* (FILE_CREATE, FILE_DIRECTORY_FILE).
static napi_value mkdir_at(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 2, argv)) return NULL;
  HANDLE dir, handle;
  WCHAR name[NAME_UNITS + 1];
  size_t units;
  if (!handle_of(env, argv[0], &dir, "mkdirat") || !named(env, argv[1], name, &units, "mkdirat")) return NULL;
  if (!open_at(env, dir, name, units, FILE_LIST_DIRECTORY, FILE_CREATE, FILE_DIRECTORY_FILE,
               FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, FILE_ATTRIBUTE_NORMAL, false, &handle, "mkdirat")) {
    return NULL;
  }
  CloseHandle(handle);
  return NULL;
}

// NtSetInformationFile with the *ex* class, and where the volume takes no POSIX form (FAT and exFAT answer
// STATUS_INVALID_PARAMETER or STATUS_NOT_SUPPORTED), with the older *plain* class and *plainInfo*, as Go's
// os.Root does for a removal.
static NTSTATUS set_information(HANDLE handle, ULONG ex, void *exInfo, ULONG exLength, ULONG plain, void *plainInfo, ULONG plainLength) {
  IO_STATUS_BLOCK io;
  NTSTATUS status = nt_set_information_file(handle, &io, exInfo, exLength, ex);
  if (status == (NTSTATUS)0xC000000DL || status == (NTSTATUS)0xC00000BBL) status = nt_set_information_file(handle, &io, plainInfo, plainLength, plain);
  return status;
}

// unlinkAt(dir, name, folder): a file, or with *folder* a folder, removed from the folder open as *dir*, as POSIX removes
// one, its name gone at once (FileDispositionInformationEx: FILE_DISPOSITION_FLAG_DELETE | _POSIX_SEMANTICS |
// _IGNORE_READONLY_ATTRIBUTE). A reparse point is refused, as every one the walk meets.
static napi_value unlink_at(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 3, argv)) return NULL;
  HANDLE dir, handle;
  WCHAR name[NAME_UNITS + 1];
  size_t units;
  bool folder;
  if (!handle_of(env, argv[0], &dir, "unlinkat") || !named(env, argv[1], name, &units, "unlinkat") || !truth(env, argv[2], &folder)) return NULL;
  if (!open_at(env, dir, name, units, DELETE, FILE_OPEN, folder ? FILE_DIRECTORY_FILE : FILE_NON_DIRECTORY_FILE,
               FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, 0, false, &handle, "unlinkat")) {
    return NULL;
  }
  ULONG flags = DISPOSITION_DELETE | DISPOSITION_POSIX_SEMANTICS | DISPOSITION_IGNORE_READONLY_ATTRIBUTE;
  BOOLEAN delete_file = TRUE;
  NTSTATUS status = set_information(handle, CLASS_DISPOSITION_EX, &flags, sizeof flags, CLASS_DISPOSITION, &delete_file, sizeof delete_file);
  CloseHandle(handle);
  return NT_SUCCESS(status) ? NULL : failed_nt(env, status, "unlinkat");
}

// The FILE_RENAME_INFORMATION or FILE_LINK_INFORMATION for *to* in the folder *dir*, which the caller frees.
static MoveInformation *move_to(HANDLE dir, const WCHAR *to, size_t units, ULONG flags, ULONG *length) {
  // Windows takes no less than the whole structure, its one WCHAR and padding with it, whatever the name's length (a
  // shorter one answers STATUS_INFO_LENGTH_MISMATCH): the name's own length is FileNameLength.
  *length = (ULONG)(sizeof(MoveInformation) + units * sizeof(WCHAR));
  MoveInformation *move = (MoveInformation *)calloc(1, *length);
  if (move == NULL) return NULL;
  move->Flags = flags;
  move->RootDirectory = dir;
  move->FileNameLength = (ULONG)(units * sizeof(WCHAR));
  memcpy(move->FileName, to, units * sizeof(WCHAR));
  return move;
}

// renameAt(fromDir, from, toDir, to, replace) and linkAt(fromDir, from, toDir, to): *from* in *fromDir* given the name
// *to* in *toDir*, by its handle, as Go's windows.Renameat and Linkat do (FileRenameInformationEx with
// FILE_RENAME_FLAG_POSIX_SEMANTICS, and FileLinkInformationEx; RootDirectory "a handle for the root directory under
// which the file will reside"). Each name is checked as every name is: a short name of another file as *to* would
// replace that file.
static napi_value move(napi_env env, napi_callback_info info, int link) {
  const char *syscall = link ? "linkat" : "renameat";
  napi_value argv[MOST_ARGS];
  if (!args(env, info, link ? 4 : 5, argv)) return NULL;
  HANDLE from_dir, to_dir, handle;
  WCHAR from[NAME_UNITS + 1], to[NAME_UNITS + 1];
  size_t from_units, to_units;
  bool replace = false;
  if (!handle_of(env, argv[0], &from_dir, syscall) || !named(env, argv[1], from, &from_units, syscall) || !handle_of(env, argv[2], &to_dir, syscall)
      || !named(env, argv[3], to, &to_units, syscall) || (!link && !truth(env, argv[4], &replace))) {
    return NULL;
  }
  if (!open_at(env, from_dir, from, from_units, link ? FILE_WRITE_ATTRIBUTES : DELETE, FILE_OPEN, 0,
               FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, 0, false, &handle, syscall)) {
    return NULL;
  }
  ULONG flags = link ? LINK_POSIX_SEMANTICS
                     : RENAME_POSIX_SEMANTICS | RENAME_IGNORE_READONLY_ATTRIBUTE | (replace ? RENAME_REPLACE_IF_EXISTS : 0);
  ULONG ex_length, plain_length;
  MoveInformation *ex = move_to(to_dir, to, to_units, flags, &ex_length);
  MoveInformation *plain = move_to(to_dir, to, to_units, !link && replace ? 1 : 0, &plain_length);
  NTSTATUS status = ex == NULL || plain == NULL
                        ? (NTSTATUS)0xC0000017L // STATUS_NO_MEMORY
                        : set_information(handle, link ? CLASS_LINK_EX : CLASS_RENAME_EX, ex, ex_length, link ? CLASS_LINK : CLASS_RENAME, plain,
                                          plain_length);
  free(ex);
  free(plain);
  CloseHandle(handle);
  return NT_SUCCESS(status) ? NULL : failed_nt(env, status, syscall);
}

static napi_value rename_at(napi_env env, napi_callback_info info) {
  return move(env, info, 0);
}

static napi_value link_at_call(napi_env env, napi_callback_info info) {
  return move(env, info, 1);
}

static void set_uint(napi_env env, napi_value object, const char *key, uint32_t value) {
  napi_value made;
  napi_create_uint32(env, value, &made);
  napi_set_named_property(env, object, key, made);
}

static void set_big(napi_env env, napi_value object, const char *key, uint64_t value) {
  napi_value made;
  napi_create_bigint_uint64(env, value, &made);
  napi_set_named_property(env, object, key, made);
}

static void set_bool(napi_env env, napi_value object, const char *key, int value) {
  napi_value made;
  napi_get_boolean(env, value != 0, &made);
  napi_set_named_property(env, object, key, made);
}

static void set_text(napi_env env, napi_value object, const char *key, const WCHAR *text, size_t units) {
  napi_value made;
  if (text == NULL) napi_get_null(env, &made);
  else napi_create_string_utf16(env, (const char16_t *)text, units, &made);
  napi_set_named_property(env, object, key, made);
}

// query(fd): what the file open as *fd* is, from its handle: its attributes and reparse tag (FileAttributeTagInfo), its
// links, size and kind (FileStandardInfo), its times (FileBasicInfo, in 100 ns since 1601), its 64-bit volume serial and
// 128-bit file ID (FileIdInfo, FILE_ID_INFO: together they "uniquely identify a file on a single computer"), its long
// name (the normalized name's last component), its 8.3 name (FileAlternateNameInformation, NtQueryInformationFile), and
// for a folder whether it compares names by case (FileCaseSensitiveInfo, FILE_CS_FLAG_CASE_SENSITIVE_DIR), null where
// its volume does not say.
static napi_value query(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 1, argv)) return NULL;
  HANDLE handle;
  if (!handle_of(env, argv[0], &handle, "query")) return NULL;
  FILE_ATTRIBUTE_TAG_INFO tag;
  FILE_STANDARD_INFO standard;
  FILE_BASIC_INFO basic;
  FILE_ID_INFO id;
  if (!GetFileInformationByHandleEx(handle, (FILE_INFO_BY_HANDLE_CLASS)BY_HANDLE_ATTRIBUTE_TAG, &tag, sizeof tag)
      || !GetFileInformationByHandleEx(handle, (FILE_INFO_BY_HANDLE_CLASS)BY_HANDLE_STANDARD, &standard, sizeof standard)
      || !GetFileInformationByHandleEx(handle, (FILE_INFO_BY_HANDLE_CLASS)BY_HANDLE_BASIC, &basic, sizeof basic)
      || !GetFileInformationByHandleEx(handle, (FILE_INFO_BY_HANDLE_CLASS)BY_HANDLE_ID, &id, sizeof id)) {
    return failed(env, "query");
  }
  napi_value result, made;
  napi_create_object(env, &result);
  set_uint(env, result, "attributes", tag.FileAttributes);
  if ((tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0) set_uint(env, result, "tag", tag.ReparseTag);
  else {
    napi_get_null(env, &made);
    napi_set_named_property(env, result, "tag", made);
  }
  set_uint(env, result, "links", standard.NumberOfLinks);
  set_big(env, result, "size", (uint64_t)standard.EndOfFile.QuadPart);
  set_big(env, result, "allocated", (uint64_t)standard.AllocationSize.QuadPart);
  set_bool(env, result, "directory", standard.Directory);
  set_bool(env, result, "deletePending", standard.DeletePending);
  set_big(env, result, "created", (uint64_t)basic.CreationTime.QuadPart);
  set_big(env, result, "accessed", (uint64_t)basic.LastAccessTime.QuadPart);
  set_big(env, result, "written", (uint64_t)basic.LastWriteTime.QuadPart);
  set_big(env, result, "changed", (uint64_t)basic.ChangeTime.QuadPart);
  set_big(env, result, "volume", id.VolumeSerialNumber);
  napi_create_buffer_copy(env, sizeof id.FileId.Identifier, id.FileId.Identifier, NULL, &made);
  napi_set_named_property(env, result, "id", made);
  size_t at, units;
  if (long_name(handle, &at, &units)) set_text(env, result, "longName", path_buffer + at, units);
  else set_text(env, result, "longName", NULL, 0);
  struct {
    ULONG length;
    WCHAR name[16];
  } alternate;
  IO_STATUS_BLOCK io;
  NTSTATUS status = nt_query_information_file(handle, &io, &alternate, sizeof alternate, CLASS_ALTERNATE_NAME);
  if (NT_SUCCESS(status) && alternate.length > 0 && alternate.length <= sizeof alternate.name) {
    set_text(env, result, "shortName", alternate.name, alternate.length / sizeof(WCHAR));
  } else {
    set_text(env, result, "shortName", NULL, 0);
  }
  ULONG sensitive = 0;
  if (standard.Directory && GetFileInformationByHandleEx(handle, (FILE_INFO_BY_HANDLE_CLASS)BY_HANDLE_CASE_SENSITIVE, &sensitive, sizeof sensitive)) {
    set_bool(env, result, "caseSensitive", (sensitive & CASE_SENSITIVE_DIR) != 0);
  } else {
    napi_get_null(env, &made);
    napi_set_named_property(env, result, "caseSensitive", made);
  }
  return result;
}

// A name as its UTF-8 bytes, a lone surrogate as WTF-8 does it (three bytes, which no UTF-8 decoder takes back): such a
// name is no text, and the file kinds leave it out as they leave out every name that is no UTF-8.
static size_t wtf8(const WCHAR *name, size_t units, unsigned char *out) {
  size_t at = 0;
  for (size_t i = 0; i < units; i++) {
    uint32_t c = name[i];
    if (c >= 0xD800 && c <= 0xDBFF && i + 1 < units && name[i + 1] >= 0xDC00 && name[i + 1] <= 0xDFFF) {
      c = 0x10000 + ((c - 0xD800) << 10) + (name[i + 1] - 0xDC00);
      i++;
    }
    if (c < 0x80) out[at++] = (unsigned char)c;
    else if (c < 0x800) {
      out[at++] = (unsigned char)(0xC0 | (c >> 6));
      out[at++] = (unsigned char)(0x80 | (c & 0x3F));
    } else if (c < 0x10000) {
      out[at++] = (unsigned char)(0xE0 | (c >> 12));
      out[at++] = (unsigned char)(0x80 | ((c >> 6) & 0x3F));
      out[at++] = (unsigned char)(0x80 | (c & 0x3F));
    } else {
      out[at++] = (unsigned char)(0xF0 | (c >> 18));
      out[at++] = (unsigned char)(0x80 | ((c >> 12) & 0x3F));
      out[at++] = (unsigned char)(0x80 | ((c >> 6) & 0x3F));
      out[at++] = (unsigned char)(0x80 | (c & 0x3F));
    }
  }
  return at;
}

// list(dir, restart): the next of the folder open as *dir*'s entries, as many as one read gives, each [its name's
// bytes, its attributes, its reparse tag or 0], "." and ".." left out; null at the end (GetFileInformationByHandleEx,
// FileIdBothDirectoryInfo, or FileIdBothDirectoryRestartInfo from the first; FILE_ID_BOTH_DIR_INFO, whose EaSize holds
// the reparse tag of an entry that is a reparse point). *dir* is a listing's own handle (reopen), whose place in the
// folder is its own.
static napi_value list(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 2, argv)) return NULL;
  HANDLE dir;
  bool restart;
  if (!handle_of(env, argv[0], &dir, "scandir") || !truth(env, argv[1], &restart)) return NULL;
  // Aligned as FILE_ID_BOTH_DIR_INFO's LARGE_INTEGERs are.
  static union {
    LONGLONG align;
    unsigned char bytes[LISTING_BYTES];
  } read;
  unsigned char *buffer = read.bytes;
  if (!GetFileInformationByHandleEx(dir, (FILE_INFO_BY_HANDLE_CLASS)(restart ? BY_HANDLE_ID_BOTH_RESTART : BY_HANDLE_ID_BOTH_DIRECTORY), buffer,
                                    sizeof read.bytes)) {
    if (GetLastError() == ERROR_NO_MORE_FILES) {
      napi_value none;
      napi_get_null(env, &none);
      return none;
    }
    return failed(env, "scandir");
  }
  napi_value entries;
  napi_create_array(env, &entries);
  uint32_t count = 0;
  static unsigned char bytes[NAME_UNITS * 4];
  for (size_t offset = 0;;) {
    const FILE_ID_BOTH_DIR_INFO *entry = (const FILE_ID_BOTH_DIR_INFO *)(buffer + offset);
    size_t units = entry->FileNameLength / sizeof(WCHAR);
    int dots = (units == 1 && entry->FileName[0] == L'.') || (units == 2 && entry->FileName[0] == L'.' && entry->FileName[1] == L'.');
    if (!dots && units <= NAME_UNITS) {
      napi_value row, part;
      napi_create_array_with_length(env, 3, &row);
      napi_create_buffer_copy(env, wtf8(entry->FileName, units, bytes), bytes, NULL, &part);
      napi_set_element(env, row, 0, part);
      napi_create_uint32(env, entry->FileAttributes, &part);
      napi_set_element(env, row, 1, part);
      napi_create_uint32(env, (entry->FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0 ? entry->EaSize : 0, &part);
      napi_set_element(env, row, 2, part);
      napi_set_element(env, entries, count++, row);
    }
    if (entry->NextEntryOffset == 0) break;
    offset += entry->NextEntryOffset;
  }
  return entries;
}

// pathOf(fd): the path the file open as *fd* has now, as Windows gives it: \\?\C:\..., every short name expanded
// (GetFinalPathNameByHandleW, FILE_NAME_NORMALIZED with VOLUME_NAME_DOS).
static napi_value path_of(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 1, argv)) return NULL;
  HANDLE handle;
  if (!handle_of(env, argv[0], &handle, "pathOf")) return NULL;
  DWORD length = GetFinalPathNameByHandleW(handle, path_buffer, PATH_UNITS, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
  if (length == 0 || length >= PATH_UNITS) return failed(env, "pathOf");
  napi_value result;
  napi_create_string_utf16(env, (const char16_t *)path_buffer, length, &result);
  return result;
}

// inheritable(fd): whether the handle under node's descriptor *fd* is one a child this process starts would be given
// (GetHandleInformation, HANDLE_FLAG_INHERIT); null for a descriptor that is not open.
static napi_value inheritable(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  if (!args(env, info, 1, argv)) return NULL;
  int32_t fd;
  napi_value result;
  if (napi_get_value_int32(env, argv[0], &fd) != napi_ok) {
    napi_throw_type_error(env, NULL, "ntat: a descriptor is a number");
    return NULL;
  }
  HANDLE handle = fd < 0 ? INVALID_HANDLE_VALUE : uv_get_osfhandle(fd);
  DWORD flags = 0;
  if (handle == INVALID_HANDLE_VALUE || handle == NULL || !GetHandleInformation(handle, &flags)) napi_get_null(env, &result);
  else napi_get_boolean(env, (flags & HANDLE_FLAG_INHERIT) != 0, &result);
  return result;
}

// handles(): how many handles this process holds (GetProcessHandleCount), for a test that counts what a thousand
// operations leave behind.
static napi_value handles(napi_env env, napi_callback_info info) {
  (void)info;
  DWORD count = 0;
  if (!GetProcessHandleCount(GetCurrentProcess(), &count)) return failed(env, "handles");
  napi_value result;
  napi_create_uint32(env, count, &result);
  return result;
}

// bootId(): this boot's number, which Windows counts up at each boot: the value BootId of the key PrefetchParameters,
// under HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management (RegGetValueW, RRF_RT_REG_DWORD). Unsure: no Microsoft
// page documents the value; a restart on a real PC measures it.
static napi_value boot_id(napi_env env, napi_callback_info info) {
  (void)info;
  DWORD value = 0, size = sizeof value;
  LSTATUS status = RegGetValueW(HKEY_LOCAL_MACHINE, L"SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                                L"BootId", RRF_RT_REG_DWORD, NULL, &value, &size);
  if (status != ERROR_SUCCESS) return thrown(env, (DWORD)status, "bootId", 0, 0, NOT_REFUSED);
  napi_value result;
  napi_create_uint32(env, value, &result);
  return result;
}

// userSid(): this process's user, as a SID's string (GetTokenInformation, TokenUser; ConvertSidToStringSidW), which names
// what is this user's alone, as the folder lock's pipe.
static napi_value user_sid(napi_env env, napi_callback_info info) {
  (void)info;
  HANDLE token;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return failed(env, "userSid");
  union {
    TOKEN_USER user;
    unsigned char room[sizeof(TOKEN_USER) + SECURITY_MAX_SID_SIZE];
  } said;
  DWORD length = 0;
  BOOL got = GetTokenInformation(token, TokenUser, &said, sizeof said, &length);
  DWORD error = GetLastError();
  CloseHandle(token);
  if (!got) return thrown(env, error, "userSid", 0, 0, NOT_REFUSED);
  LPWSTR text = NULL;
  if (!ConvertSidToStringSidW(said.user.User.Sid, &text)) return failed(env, "userSid");
  napi_value result;
  napi_create_string_utf16(env, (const char16_t *)text, wcslen(text), &result);
  LocalFree(text);
  return result;
}

static void constant(napi_env env, napi_value object, const char *key, uint32_t value) {
  set_uint(env, object, key, value);
}

NAPI_MODULE_INIT() {
  HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
  if (ntdll != NULL) {
    nt_create_file = (CreateFileCall)(void *)GetProcAddress(ntdll, "NtCreateFile");
    nt_query_information_file = (InformationCall)(void *)GetProcAddress(ntdll, "NtQueryInformationFile");
    nt_set_information_file = (InformationCall)(void *)GetProcAddress(ntdll, "NtSetInformationFile");
    rtl_nt_status_to_dos_error = (DosErrorCall)(void *)GetProcAddress(ntdll, "RtlNtStatusToDosError");
    rtl_set_placeholder_mode = (PlaceholderCall)(void *)GetProcAddress(ntdll, "RtlSetProcessPlaceholderCompatibilityMode");
  }
  if (nt_create_file == NULL || nt_query_information_file == NULL || nt_set_information_file == NULL || rtl_nt_status_to_dos_error == NULL) {
    napi_throw_error(env, NULL, "ntat: ntdll lacks NtCreateFile, NtQueryInformationFile, NtSetInformationFile or RtlNtStatusToDosError");
    return NULL;
  }
  const struct {
    const char *name;
    napi_callback call;
  } calls[] = {
      {"openRoot", open_root}, {"openAt", open_at_call}, {"reopen", reopen},       {"mkdirAt", mkdir_at},
      {"unlinkAt", unlink_at}, {"renameAt", rename_at},  {"linkAt", link_at_call}, {"query", query},
      {"list", list},          {"pathOf", path_of},      {"inheritable", inheritable}, {"handles", handles},
      {"bootId", boot_id},     {"userSid", user_sid},
  };
  for (size_t i = 0; i < sizeof calls / sizeof calls[0]; i++) {
    napi_value fn;
    napi_create_function(env, calls[i].name, NAPI_AUTO_LENGTH, calls[i].call, NULL, &fn);
    napi_set_named_property(env, exports, calls[i].name, fn);
  }
  // The numbers ntat takes, as Windows's headers define them (winnt.h, winternl.h and ntdll's above).
  napi_value constants;
  napi_create_object(env, &constants);
  constant(env, constants, "FILE_READ_DATA", FILE_READ_DATA);
  constant(env, constants, "FILE_LIST_DIRECTORY", FILE_LIST_DIRECTORY);
  constant(env, constants, "FILE_WRITE_DATA", FILE_WRITE_DATA);
  constant(env, constants, "FILE_APPEND_DATA", FILE_APPEND_DATA);
  constant(env, constants, "FILE_TRAVERSE", FILE_TRAVERSE);
  constant(env, constants, "FILE_READ_ATTRIBUTES", FILE_READ_ATTRIBUTES);
  constant(env, constants, "FILE_WRITE_ATTRIBUTES", FILE_WRITE_ATTRIBUTES);
  constant(env, constants, "FILE_GENERIC_READ", FILE_GENERIC_READ);
  constant(env, constants, "FILE_GENERIC_WRITE", FILE_GENERIC_WRITE);
  constant(env, constants, "DELETE", DELETE);
  constant(env, constants, "SYNCHRONIZE", SYNCHRONIZE);
  constant(env, constants, "FILE_SHARE_READ", FILE_SHARE_READ);
  constant(env, constants, "FILE_SHARE_WRITE", FILE_SHARE_WRITE);
  constant(env, constants, "FILE_SHARE_DELETE", FILE_SHARE_DELETE);
  constant(env, constants, "FILE_OPEN", FILE_OPEN);
  constant(env, constants, "FILE_CREATE", FILE_CREATE);
  constant(env, constants, "FILE_OPEN_IF", FILE_OPEN_IF);
  constant(env, constants, "FILE_OVERWRITE", FILE_OVERWRITE);
  constant(env, constants, "FILE_OVERWRITE_IF", FILE_OVERWRITE_IF);
  constant(env, constants, "FILE_DIRECTORY_FILE", FILE_DIRECTORY_FILE);
  constant(env, constants, "FILE_NON_DIRECTORY_FILE", FILE_NON_DIRECTORY_FILE);
  constant(env, constants, "FILE_ATTRIBUTE_READONLY", FILE_ATTRIBUTE_READONLY);
  constant(env, constants, "FILE_ATTRIBUTE_DIRECTORY", FILE_ATTRIBUTE_DIRECTORY);
  constant(env, constants, "FILE_ATTRIBUTE_NORMAL", FILE_ATTRIBUTE_NORMAL);
  constant(env, constants, "FILE_ATTRIBUTE_REPARSE_POINT", FILE_ATTRIBUTE_REPARSE_POINT);
  constant(env, constants, "IO_REPARSE_TAG_MOUNT_POINT", IO_REPARSE_TAG_MOUNT_POINT);
  constant(env, constants, "IO_REPARSE_TAG_SYMLINK", IO_REPARSE_TAG_SYMLINK);
  constant(env, constants, "IO_REPARSE_TAG_CLOUD", CLOUD_TAG);
  constant(env, constants, "IO_REPARSE_TAG_CLOUD_MASK", CLOUD_MASK);
  napi_set_named_property(env, exports, "constants", constants);
  return exports;
}
