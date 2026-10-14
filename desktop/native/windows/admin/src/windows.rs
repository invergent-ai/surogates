//! An update applied, as Windows does each step: the app's process waited for (OpenProcess, WaitForSingleObject); the
//! release's files copied, through no reparse point, into a folder of %ProgramData%\Surogate\apply that only
//! administrators and SYSTEM reach, its owner the Administrators, so that its maker's own account cannot change it
//! (SHGetKnownFolderPath, CreateDirectoryW and CreateFileW with a descriptor of their own), the MSI hashed as it is
//! copied (CNG's SHA-256); the copies checked (release.rs, and WinVerifyTrust for the MSI's signer, through
//! surogate-helper's verify); and Windows Installer run on the copy (msiexec, from the system's own folder).

use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::{Read, Write};
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::io::FromRawHandle;
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};

use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, LocalFree, ERROR_INVALID_PARAMETER, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Security::Authorization::{ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1};
use windows_sys::Win32::Security::Cryptography::{BCryptCreateHash, BCryptDestroyHash, BCryptFinishHash, BCryptHashData, BCRYPT_ALG_HANDLE, BCRYPT_HASH_HANDLE};
use windows_sys::Win32::Security::{PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES};
use windows_sys::Win32::Storage::FileSystem::{
    CreateDirectoryW, CreateFileW, FileAttributeTagInfo, GetFileInformationByHandleEx, CREATE_NEW, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL,
    FILE_ATTRIBUTE_REPARSE_POINT, FILE_ATTRIBUTE_TAG_INFO, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_SEQUENTIAL_SCAN, FILE_GENERIC_READ,
    FILE_GENERIC_WRITE, FILE_READ_ATTRIBUTES, FILE_SHARE_READ, OPEN_EXISTING, READ_CONTROL,
};
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::System::LibraryLoader::GetModuleFileNameW;
use windows_sys::Win32::System::SystemInformation::{GetSystemDirectoryW, GetSystemWindowsDirectoryW};
use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE};
use windows_sys::Win32::UI::Shell::{FOLDERID_ProgramData, SHGetKnownFolderPath};

use crate::args::Apply;
use crate::release::{checked, MANIFEST_MAX, SIGNATURE_SIZE};
use crate::{exit, keys, QUIT_SECONDS};

// The folder an update is checked in, and each file of it: the Administrators', which only they and SYSTEM reach, and
// none of its maker's own.
const FOLDER: &str = "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)";
const FILE: &str = "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)";
// CNG's own handle of SHA-256, which needs no provider opened (BCRYPT_SHA256_ALG_HANDLE, bcrypt.h).
const SHA256: BCRYPT_ALG_HANDLE = 0x41 as BCRYPT_ALG_HANDLE;
// WinVerifyTrust's TRUST_E_NOSIGNATURE: a program with no signature, as a development build is.
const NO_SIGNATURE: i32 = 0x800B_0100_u32 as i32;
// Windows Installer's: a success that needs a restart to finish.
const RESTART_NEEDED: i32 = 3010;
// The most the MSI is: no release is larger.
const MSI_MAX: u64 = 4 << 30;

type Failure = (i32, String);

fn failed(words: impl Into<String>) -> Failure {
    (exit::FAILED, words.into())
}

fn not_this_apps(words: impl Into<String>) -> Failure {
    (exit::NOT_THIS_APPS, words.into())
}

fn wide(text: impl AsRef<OsStr>) -> Vec<u16> {
    text.as_ref().encode_wide().chain(std::iter::once(0)).collect()
}

fn last_error() -> u32 {
    unsafe { GetLastError() }
}

struct Handle(HANDLE);

impl Drop for Handle {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

/// A descriptor made from *sddl*, freed with it.
struct Descriptor(PSECURITY_DESCRIPTOR);

impl Descriptor {
    fn of(sddl: &str) -> Result<Self, Failure> {
        let mut descriptor: PSECURITY_DESCRIPTOR = null_mut();
        // SAFETY: the string outlives the call; the descriptor is freed by Drop.
        if unsafe { ConvertStringSecurityDescriptorToSecurityDescriptorW(wide(sddl).as_ptr(), SDDL_REVISION_1, &mut descriptor, null_mut()) } == 0 {
            return Err(failed(format!("the descriptor {sddl} could not be made: Windows error {}", last_error())));
        }
        Ok(Descriptor(descriptor))
    }

    fn attributes(&self) -> SECURITY_ATTRIBUTES {
        SECURITY_ATTRIBUTES { nLength: size_of::<SECURITY_ATTRIBUTES>() as u32, lpSecurityDescriptor: self.0, bInheritHandle: 0 }
    }
}

impl Drop for Descriptor {
    fn drop(&mut self) {
        unsafe { LocalFree(self.0) };
    }
}

/// Whether the process *pid* has ended, waiting up to *seconds* for it; one that is not there has.
fn ended(pid: u32, seconds: u32) -> bool {
    // SAFETY: a handle opened here and closed by Handle.
    let process = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    if process.is_null() {
        return last_error() == ERROR_INVALID_PARAMETER;
    }
    let process = Handle(process);
    unsafe { WaitForSingleObject(process.0, seconds * 1000) == 0 }
}

/// %ProgramData%, as Windows knows it, never as an environment names it.
fn program_data() -> Result<PathBuf, Failure> {
    let mut path = null_mut();
    // SAFETY: Windows writes a string it allocated, freed here once read.
    let status = unsafe { SHGetKnownFolderPath(&FOLDERID_ProgramData, 0, null_mut(), &mut path) };
    if status != 0 {
        return Err(failed(format!("Windows named no ProgramData folder: error 0x{status:08x}")));
    }
    let units = unsafe { std::slice::from_raw_parts(path, (0..).take_while(|&at| *path.add(at) != 0).count()) };
    let found = PathBuf::from(OsString::from_wide(units));
    unsafe { CoTaskMemFree(path.cast()) };
    Ok(found)
}

fn folder_of(call: unsafe extern "system" fn(*mut u16, u32) -> u32, what: &str) -> Result<PathBuf, Failure> {
    let mut buffer = vec![0u16; 1024];
    // SAFETY: the buffer is as long as said.
    let length = unsafe { call(buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
    if length == 0 || length >= buffer.len() {
        return Err(failed(format!("Windows named no {what} folder: Windows error {}", last_error())));
    }
    Ok(PathBuf::from(OsString::from_wide(&buffer[..length])))
}

/// This program's own path.
fn own_path() -> Result<PathBuf, Failure> {
    let mut buffer = vec![0u16; 32_768];
    // SAFETY: the buffer is as long as said; a null module is this program's.
    let length = unsafe { GetModuleFileNameW(null_mut(), buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
    if length == 0 || length >= buffer.len() {
        return Err(failed(format!("this program's own path could not be read: Windows error {}", last_error())));
    }
    Ok(PathBuf::from(OsString::from_wide(&buffer[..length])))
}

/// *path* opened for *access*, through no reparse point, and its attributes.
fn open(path: &Path, access: u32, flags: u32) -> Result<(Handle, u32), String> {
    // SAFETY: the path outlives the call; the handle is closed by Handle.
    let handle = unsafe { CreateFileW(wide(path).as_ptr(), access, FILE_SHARE_READ, null(), OPEN_EXISTING, flags | FILE_FLAG_OPEN_REPARSE_POINT, null_mut()) };
    if handle == INVALID_HANDLE_VALUE {
        return Err(format!("{} could not be opened: Windows error {}", path.display(), last_error()));
    }
    let handle = Handle(handle);
    let mut tag = FILE_ATTRIBUTE_TAG_INFO { FileAttributes: 0, ReparseTag: 0 };
    // SAFETY: as above, into a structure of the class's size.
    if unsafe { GetFileInformationByHandleEx(handle.0, FileAttributeTagInfo, (&raw mut tag).cast(), size_of::<FILE_ATTRIBUTE_TAG_INFO>() as u32) } == 0 {
        return Err(format!("{} could not be read: Windows error {}", path.display(), last_error()));
    }
    if tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(format!("{} is a link", path.display()));
    }
    Ok((handle, tag.FileAttributes))
}

/// *path*, a folder or a file of the install, refused unless it is an administrator's word (owners) and no link.
fn installers(path: &Path, folder: bool) -> Result<Handle, Failure> {
    let flags = if folder { FILE_FLAG_BACKUP_SEMANTICS } else { 0 };
    let (handle, attributes) = open(path, READ_CONTROL | FILE_READ_ATTRIBUTES | if folder { 0 } else { FILE_GENERIC_READ }, flags).map_err(failed)?;
    if (attributes & FILE_ATTRIBUTE_DIRECTORY != 0) != folder {
        return Err(failed(format!("{} is not the {} the install made", path.display(), if folder { "folder" } else { "file" })));
    }
    // SAFETY: the handle is open, with READ_CONTROL, until it is returned or dropped below.
    let said = unsafe { owners::of(handle.0) }.map_err(|why| failed(format!("{}: {why}", path.display())))?;
    match said.refusal(&path.display().to_string()) {
        Some(why) => Err(failed(format!("{why}: install Surogate again"))),
        None => Ok(handle),
    }
}

/// A file's whole text, read from a handle `installers` gave.
fn read_all(handle: Handle, max: u64, path: &Path) -> Result<Vec<u8>, Failure> {
    // SAFETY: the handle is this call's alone from here; File closes it.
    let file = unsafe { File::from_raw_handle(handle.0.cast()) };
    std::mem::forget(handle);
    let mut bytes = Vec::new();
    file.take(max + 1).read_to_end(&mut bytes).map_err(|error| failed(format!("{} could not be read: {error}", path.display())))?;
    if bytes.len() as u64 > max {
        return Err(failed(format!("{} is larger than the install writes it", path.display())));
    }
    Ok(bytes)
}

/// A SHA-256 being made, through CNG.
struct Sha256(BCRYPT_HASH_HANDLE);

impl Sha256 {
    fn new() -> Result<Self, Failure> {
        let mut hash: BCRYPT_HASH_HANDLE = null_mut();
        // SAFETY: CNG keeps the hash's object itself; it is destroyed by Drop.
        let status = unsafe { BCryptCreateHash(SHA256, &mut hash, null_mut(), 0, null(), 0, 0) };
        if status != 0 {
            return Err(failed(format!("a SHA-256 could not be begun: status 0x{status:08x}")));
        }
        Ok(Sha256(hash))
    }

    fn add(&mut self, bytes: &[u8]) -> Result<(), Failure> {
        // SAFETY: the bytes outlive the call.
        let status = unsafe { BCryptHashData(self.0, bytes.as_ptr(), bytes.len() as u32, 0) };
        if status != 0 { Err(failed(format!("the MSI could not be hashed: status 0x{status:08x}"))) } else { Ok(()) }
    }

    fn hex(self) -> Result<String, Failure> {
        let mut digest = [0u8; 32];
        // SAFETY: the digest is SHA-256's size.
        let status = unsafe { BCryptFinishHash(self.0, digest.as_mut_ptr(), digest.len() as u32, 0) };
        if status != 0 {
            return Err(failed(format!("the MSI's hash could not be finished: status 0x{status:08x}")));
        }
        Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
    }
}

impl Drop for Sha256 {
    fn drop(&mut self) {
        unsafe { BCryptDestroyHash(self.0) };
    }
}

/// *from*, the user's file, copied to the new file *to*, of the Administrators' descriptor: through no reparse point,
/// of *max* bytes at most, and hashed as it is written. Its size and SHA-256.
fn copy(from: &OsStr, to: &Path, max: u64) -> Result<(u64, String), Failure> {
    let from = Path::new(from);
    let (source, attributes) = open(from, FILE_GENERIC_READ, FILE_FLAG_SEQUENTIAL_SCAN).map_err(failed)?;
    if attributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
        return Err(failed(format!("{} is not a file", from.display())));
    }
    let descriptor = Descriptor::of(FILE)?;
    let attributes = descriptor.attributes();
    // SAFETY: the path and the attributes outlive the call; File closes the handle.
    let target = unsafe { CreateFileW(wide(to).as_ptr(), FILE_GENERIC_WRITE, 0, &attributes, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, null_mut()) };
    if target == INVALID_HANDLE_VALUE {
        return Err(failed(format!("{} could not be made: Windows error {}", to.display(), last_error())));
    }
    let mut target = unsafe { File::from_raw_handle(target.cast()) };
    let mut source = unsafe { File::from_raw_handle(source.0.cast()) };
    let mut hash = Sha256::new()?;
    let mut size = 0u64;
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let read = source.read(&mut buffer).map_err(|error| failed(format!("{} could not be read: {error}", from.display())))?;
        if read == 0 {
            break;
        }
        size += read as u64;
        if size > max {
            return Err(not_this_apps(format!("{} is larger than a release's is", from.display())));
        }
        hash.add(&buffer[..read])?;
        target.write_all(&buffer[..read]).map_err(|error| failed(format!("{} could not be written: {error}", to.display())))?;
    }
    target.sync_all().map_err(|error| failed(format!("{} could not be written: {error}", to.display())))?;
    Ok((size, hash.hex()?))
}

/// The installed version and its channel, from the install record the MSI wrote.
fn installed(record: &Path) -> Result<(String, String), Failure> {
    let handle = installers(record, false)?;
    let text = read_all(handle, MANIFEST_MAX as u64, record)?;
    let read: serde_json::Value = serde_json::from_slice(&text).map_err(|_| failed(format!("{} is not the install's record", record.display())))?;
    let field = |name: &str| read.get(name).and_then(serde_json::Value::as_str).filter(|value| !value.is_empty()).map(str::to_string);
    match (field("version"), field("channel")) {
        (Some(version), Some(channel)) => Ok((version, channel)),
        _ => Err(failed(format!("{} names no installed version or channel: install Surogate again", record.display()))),
    }
}

/// Whom the MSI must be signed by: this program's own signer, or a development build's test signer.
fn signer() -> Result<String, Failure> {
    let own = own_path()?;
    let verdict = surogate_helper::windows::verify::verdict(own.as_os_str()).map_err(failed)?;
    if verdict.trusted {
        return verdict.signer.ok_or_else(|| not_this_apps("this program's signer has no name"));
    }
    if verdict.status == NO_SIGNATURE {
        // Unsigned, as a development build is: its test's signer alone.
        return std::env::var("SUROGATE_TEST_SIGNER").ok().filter(|name| !name.is_empty()).ok_or_else(|| not_this_apps("this program is not signed, and no test signer is named"));
    }
    Err(not_this_apps(format!("this program's own signature does not check out: 0x{:08x}", verdict.status)))
}

fn run(apply: &Apply) -> Result<String, Failure> {
    if !ended(apply.wait, QUIT_SECONDS) {
        return Err((exit::DID_NOT_QUIT, format!("Surogate did not quit within {QUIT_SECONDS} s, so nothing was installed")));
    }
    let data = program_data()?.join("Surogate");
    installers(&data, true)?;
    let updates = data.join("apply");
    installers(&updates, true)?;
    let folder = updates.join(&apply.version);
    if std::fs::symlink_metadata(&folder).is_ok() {
        std::fs::remove_dir_all(&folder).map_err(|error| failed(format!("{} could not be emptied: {error}", folder.display())))?;
    }
    let descriptor = Descriptor::of(FOLDER)?;
    let attributes = descriptor.attributes();
    // SAFETY: the path and the attributes outlive the call.
    if unsafe { CreateDirectoryW(wide(&folder).as_ptr(), &attributes) } == 0 {
        return Err(failed(format!("{} could not be made: Windows error {}", folder.display(), last_error())));
    }
    // Copies of the user's files, read once each; from here on only the copies are read.
    let manifest = folder.join("manifest.json");
    let signature = folder.join("manifest.json.sig");
    let msi = folder.join("release.msi");
    copy(&apply.manifest, &manifest, MANIFEST_MAX as u64)?;
    copy(&apply.signature, &signature, SIGNATURE_SIZE as u64)?;
    let (size, sha256) = copy(&apply.msi, &msi, MSI_MAX)?;
    let read = |path: &Path| std::fs::read(path).map_err(|error| failed(format!("{} could not be read: {error}", path.display())));
    let (manifest, signature) = (read(&manifest)?, read(&signature)?);
    let (version, channel) = installed(&data.join("install.json"))?;
    // The keys the install trusts, beside this program: <install>\bin\surogate-admin.exe, <install>\resources\release-keys.
    let install = own_path()?.parent().and_then(Path::parent).map(Path::to_path_buf).ok_or_else(|| failed("this program is in no install"))?;
    let keys_path = install.join("resources").join("release-keys");
    let keys_text = read_all(installers(&keys_path, false)?, 64 * 1024, &keys_path)?;
    let trusted = keys::listed(&String::from_utf8_lossy(&keys_text));
    if trusted.is_empty() {
        return Err(failed(format!("{} lists no release key: install Surogate again", keys_path.display())));
    }
    let release = checked(&manifest, &signature, &trusted, &apply.version, &channel, &version).map_err(not_this_apps)?;
    if release.size != size || release.sha256 != sha256 {
        return Err(not_this_apps("the MSI is not the one the release's manifest names"));
    }
    let expected = signer()?;
    let verdict = surogate_helper::windows::verify::verdict(msi.as_os_str()).map_err(failed)?;
    if !verdict.trusted || verdict.signer.as_deref() != Some(expected.as_str()) {
        return Err(not_this_apps(format!("the MSI is signed by {}, not by {expected}", verdict.signer.as_deref().unwrap_or("no one Windows trusts"))));
    }
    // Windows Installer, from the system's own folder, on the copy; its log where it runs, in Windows's Temp.
    let msiexec = folder_of(GetSystemDirectoryW, "system")?.join("msiexec.exe");
    let log = folder_of(GetSystemWindowsDirectoryW, "Windows")?.join("Temp").join(format!("surogate-update-{}.log", apply.version));
    let status = std::process::Command::new(&msiexec)
        .args([OsStr::new("/i"), msi.as_os_str(), OsStr::new("/qn"), OsStr::new("/norestart"), OsStr::new("/l*v"), log.as_os_str()])
        .status()
        .map_err(|error| failed(format!("Windows Installer could not be started: {error}")))?;
    let code = status.code().unwrap_or(-1);
    if code != 0 && code != RESTART_NEEDED {
        return Err((exit::INSTALLER_FAILED, format!("Windows Installer ended {code}, and Surogate {} was not installed: its log is {}", apply.version, log.display())));
    }
    // What earlier updates left: only this one's copies stay, which a repair of it may read.
    for entry in std::fs::read_dir(&updates).map_err(|error| failed(format!("{} could not be read: {error}", updates.display())))?.flatten() {
        if entry.file_name() != OsStr::new(&apply.version) {
            let _ = std::fs::remove_dir_all(entry.path()).or_else(|_| std::fs::remove_file(entry.path()));
        }
    }
    Ok(if code == RESTART_NEEDED { format!("Surogate {} is installed, and a restart of Windows finishes it", apply.version) } else { String::new() })
}

/// The update applied: its exit code, and its words.
pub fn apply(apply: &Apply) -> (i32, String) {
    match run(apply) {
        Ok(words) => (exit::INSTALLED, words),
        Err((code, words)) => (code, words),
    }
}
