using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

// Drives `spec watch` from the installed wheel through the Windows
// pseudoconsole API. This is intentionally a small checked-in harness rather
// than a terminal-emulator dependency: the release proof must establish that
// stdin/stdout are genuine console handles and that the production Textual app
// works in the logged-on desktop session.
public static class WatchConptyProof
{
    private const uint EXTENDED_STARTUPINFO_PRESENT = 0x00080000;
    private const uint CREATE_SUSPENDED = 0x00000004;
    private const uint PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016;
    private const int STARTF_USESTDHANDLES = 0x00000100;
    private const uint TH32CS_SNAPPROCESS = 0x00000002;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 258;
    private const uint INVALID_RESUME_RESULT = 0xffffffff;

    [StructLayout(LayoutKind.Sequential)]
    private struct COORD
    {
        public short X;
        public short Y;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public int dwX;
        public int dwY;
        public int dwXSize;
        public int dwYSize;
        public int dwXCountChars;
        public int dwYCountChars;
        public int dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFOEX
    {
        public STARTUPINFO StartupInfo;
        public IntPtr lpAttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SECURITY_ATTRIBUTES
    {
        public int nLength;
        public IntPtr lpSecurityDescriptor;
        public int bInheritHandle;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PROCESSENTRY32
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    private sealed class ProcessIdentity
    {
        public int Pid;
        public long StartTimeUtcTicks;
        public string Name = "";
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreatePipe(
        out SafeFileHandle hReadPipe,
        out SafeFileHandle hWritePipe,
        IntPtr lpPipeAttributes,
        uint nSize
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern int CreatePseudoConsole(
        COORD size,
        SafeFileHandle hInput,
        SafeFileHandle hOutput,
        uint dwFlags,
        out IntPtr phPC
    );

    [DllImport("kernel32.dll")]
    private static extern void ClosePseudoConsole(IntPtr hPC);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool InitializeProcThreadAttributeList(
        IntPtr lpAttributeList,
        int dwAttributeCount,
        int dwFlags,
        ref IntPtr lpSize
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool UpdateProcThreadAttribute(
        IntPtr lpAttributeList,
        uint dwFlags,
        IntPtr attribute,
        IntPtr lpValue,
        IntPtr cbSize,
        IntPtr lpPreviousValue,
        IntPtr lpReturnSize
    );

    [DllImport("kernel32.dll")]
    private static extern void DeleteProcThreadAttributeList(IntPtr lpAttributeList);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcess(
        string lpApplicationName,
        string lpCommandLine,
        ref SECURITY_ATTRIBUTES lpProcessAttributes,
        ref SECURITY_ATTRIBUTES lpThreadAttributes,
        bool bInheritHandles,
        uint dwCreationFlags,
        IntPtr lpEnvironment,
        string lpCurrentDirectory,
        [In] ref STARTUPINFOEX lpStartupInfo,
        out PROCESS_INFORMATION lpProcessInformation
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr hThread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr hJob,
        int jobObjectInfoClass,
        IntPtr lpJobObjectInfo,
        uint cbJobObjectInfoLength
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint dwFlags, uint th32ProcessID);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool Process32First(IntPtr hSnapshot, ref PROCESSENTRY32 lppe);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool Process32Next(IntPtr hSnapshot, ref PROCESSENTRY32 lppe);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ProcessIdToSessionId(uint dwProcessId, out uint pSessionId);

    private static readonly object OutputLock = new object();
    private static readonly StringBuilder Output = new StringBuilder();
    private static readonly Dictionary<string, ProcessIdentity> Observed =
        new Dictionary<string, ProcessIdentity>(StringComparer.Ordinal);

    private static int RootPid;
    private static bool ProviderObserved;
    private static ProcessIdentity ProviderIdentity;

    private static void Require(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }

    private static void RequireWin32(bool condition, string operation)
    {
        if (!condition)
        {
            throw new System.ComponentModel.Win32Exception(
                Marshal.GetLastWin32Error(), operation
            );
        }
    }

    private static bool IsLowerHex(string value)
    {
        foreach (char character in value)
        {
            if (!((character >= '0' && character <= '9')
                || (character >= 'a' && character <= 'f')))
            {
                return false;
            }
        }
        return true;
    }

    private static string QuoteArgument(string value)
    {
        if (value.Length > 0 && value.IndexOfAny(new char[] { ' ', '\t', '"' }) < 0)
        {
            return value;
        }

        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                slashes += 1;
            }
            else if (character == '"')
            {
                result.Append('\\', (slashes * 2) + 1);
                result.Append('"');
                slashes = 0;
            }
            else
            {
                result.Append('\\', slashes);
                slashes = 0;
                result.Append(character);
            }
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    private static string BuildCommandLine(string specExe, string repoRoot)
    {
        string pythonExe = Path.Combine(Path.GetDirectoryName(specExe), "python.exe");
        Require(File.Exists(pythonExe), "installed wheel interpreter does not exist: " + pythonExe);
        return QuoteArgument(pythonExe)
            + " -I -m spec_runtime.cli watch --interval 1 --agent codex --repo-root "
            + QuoteArgument(repoRoot);
    }

    private static void ReadOutput(StreamReader reader)
    {
        char[] buffer = new char[4096];
        try
        {
            int count;
            while ((count = reader.Read(buffer, 0, buffer.Length)) > 0)
            {
                lock (OutputLock)
                {
                    Output.Append(buffer, 0, count);
                }
            }
        }
        catch (IOException)
        {
            // Closing the pseudoconsole terminates the output pipe.
        }
        catch (ObjectDisposedException)
        {
        }
    }

    private static int OutputLength()
    {
        lock (OutputLock)
        {
            return Output.Length;
        }
    }

    private static string OutputSince(int offset)
    {
        lock (OutputLock)
        {
            if (offset < 0 || offset > Output.Length)
            {
                offset = 0;
            }
            return Output.ToString(offset, Output.Length - offset);
        }
    }

    private static List<PROCESSENTRY32> ProcessSnapshot()
    {
        IntPtr snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snapshot == new IntPtr(-1))
        {
            throw new System.ComponentModel.Win32Exception(
                Marshal.GetLastWin32Error(), "CreateToolhelp32Snapshot"
            );
        }
        try
        {
            List<PROCESSENTRY32> entries = new List<PROCESSENTRY32>();
            PROCESSENTRY32 entry = new PROCESSENTRY32();
            entry.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32));
            if (!Process32First(snapshot, ref entry))
            {
                throw new System.ComponentModel.Win32Exception(
                    Marshal.GetLastWin32Error(), "Process32First"
                );
            }
            do
            {
                entries.Add(entry);
                entry = new PROCESSENTRY32();
                entry.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32));
            }
            while (Process32Next(snapshot, ref entry));
            return entries;
        }
        finally
        {
            CloseHandle(snapshot);
        }
    }

    private static bool IsDescendant(
        uint pid,
        Dictionary<uint, uint> parents,
        uint rootPid
    )
    {
        HashSet<uint> visited = new HashSet<uint>();
        uint current = pid;
        while (parents.ContainsKey(current) && visited.Add(current))
        {
            uint parent = parents[current];
            if (parent == rootPid)
            {
                return true;
            }
            if (parent == 0 || parent == current)
            {
                return false;
            }
            current = parent;
        }
        return false;
    }

    private static ProcessIdentity IdentityFor(PROCESSENTRY32 entry)
    {
        try
        {
            using (Process process = Process.GetProcessById((int)entry.th32ProcessID))
            {
                return new ProcessIdentity
                {
                    Pid = (int)entry.th32ProcessID,
                    StartTimeUtcTicks = process.StartTime.ToUniversalTime().Ticks,
                    Name = entry.szExeFile ?? ""
                };
            }
        }
        catch (ArgumentException)
        {
            return null;
        }
        catch (InvalidOperationException)
        {
            return null;
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return null;
        }
    }

    private static void TrackDescendants()
    {
        if (RootPid <= 0)
        {
            return;
        }
        List<PROCESSENTRY32> entries = ProcessSnapshot();
        Dictionary<uint, uint> parents = new Dictionary<uint, uint>();
        foreach (PROCESSENTRY32 entry in entries)
        {
            parents[entry.th32ProcessID] = entry.th32ParentProcessID;
        }
        foreach (PROCESSENTRY32 entry in entries)
        {
            if (entry.th32ProcessID == (uint)RootPid
                || !IsDescendant(entry.th32ProcessID, parents, (uint)RootPid))
            {
                continue;
            }
            ProcessIdentity identity = IdentityFor(entry);
            if (identity == null)
            {
                continue;
            }
            string key = identity.Pid + ":" + identity.StartTimeUtcTicks;
            Observed[key] = identity;
            if (identity.Name.StartsWith("codex", StringComparison.OrdinalIgnoreCase))
            {
                ProviderObserved = true;
                ProviderIdentity = identity;
            }
        }
    }

    private static bool IdentityIsAlive(ProcessIdentity identity)
    {
        try
        {
            using (Process process = Process.GetProcessById(identity.Pid))
            {
                return process.StartTime.ToUniversalTime().Ticks == identity.StartTimeUtcTicks
                    && !process.HasExited;
            }
        }
        catch (ArgumentException)
        {
            return false;
        }
        catch (InvalidOperationException)
        {
            return false;
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return false;
        }
    }

    private static bool WaitForAll(int offset, int timeoutSeconds, params string[] needles)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTime.UtcNow < deadline)
        {
            TrackDescendants();
            string text = OutputSince(offset);
            bool found = true;
            foreach (string needle in needles)
            {
                if (text.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
                {
                    found = false;
                    break;
                }
            }
            if (found)
            {
                return true;
            }
            Thread.Sleep(100);
        }
        TrackDescendants();
        return false;
    }

    private static string FirstStatus(int offset)
    {
        string text = OutputSince(offset);
        string[] values = new string[]
        {
            "needs-input", "running", "waiting", "blocked", "failed", "stale", "passed", "pending"
        };
        foreach (string value in values)
        {
            if (text.IndexOf(value, StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return value;
            }
        }
        return "";
    }

    private static bool WaitForProviderExit(int timeoutSeconds)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTime.UtcNow < deadline)
        {
            TrackDescendants();
            if (ProviderObserved && CountAliveProviders() == 0)
            {
                return true;
            }
            Thread.Sleep(100);
        }
        return ProviderObserved && CountAliveProviders() == 0;
    }

    private static int CountAliveProviders()
    {
        int count = 0;
        foreach (ProcessIdentity identity in Observed.Values)
        {
            if (identity.Name.StartsWith("codex", StringComparison.OrdinalIgnoreCase)
                && IdentityIsAlive(identity))
            {
                count += 1;
            }
        }
        return count;
    }

    private static int CountAlive(IEnumerable<ProcessIdentity> identities)
    {
        int count = 0;
        foreach (ProcessIdentity identity in identities)
        {
            if (IdentityIsAlive(identity))
            {
                count += 1;
            }
        }
        return count;
    }

    private static int WaitForObservedExit(int timeoutSeconds, bool discoverNewDescendants)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        int remaining;
        do
        {
            if (discoverNewDescendants)
            {
                TrackDescendants();
            }
            remaining = CountAlive(Observed.Values);
            if (remaining == 0)
            {
                return 0;
            }
            Thread.Sleep(100);
        }
        while (DateTime.UtcNow < deadline);
        return remaining;
    }

    private static string DescribeAlive(IEnumerable<ProcessIdentity> identities)
    {
        List<string> values = new List<string>();
        foreach (ProcessIdentity identity in identities)
        {
            if (IdentityIsAlive(identity))
            {
                values.Add(identity.Name + "[" + identity.Pid + "]");
            }
        }
        return string.Join(", ", values.ToArray());
    }

    private static string CapturedOutput()
    {
        lock (OutputLock)
        {
            return Output.ToString();
        }
    }

    private static bool ProcessIsRunning(IntPtr processHandle)
    {
        if (processHandle == IntPtr.Zero)
        {
            return false;
        }
        return WaitForSingleObject(processHandle, 0) == WAIT_TIMEOUT;
    }

    private static void EmergencyCleanup(
        ref IntPtr job,
        IntPtr processHandle,
        ref IntPtr pseudoConsole
    )
    {
        // KILL_ON_JOB_CLOSE must fire before ClosePseudoConsole. A stuck client
        // can otherwise make pseudoconsole teardown wait forever and prevent
        // the ownership backstop from ever running. The output reader remains
        // active while this method kills clients and closes the ConPTY.
        if (job != IntPtr.Zero)
        {
            TerminateJobObject(job, 1);
            CloseHandle(job);
            job = IntPtr.Zero;
        }
        if (ProcessIsRunning(processHandle))
        {
            TerminateProcess(processHandle, 1);
            WaitForSingleObject(processHandle, 5000);
        }
        if (pseudoConsole != IntPtr.Zero && !ProcessIsRunning(processHandle))
        {
            ClosePseudoConsole(pseudoConsole);
            pseudoConsole = IntPtr.Zero;
        }
    }

    private static string JsonEscape(string value)
    {
        if (value == null)
        {
            return "";
        }
        StringBuilder escaped = new StringBuilder();
        foreach (char character in value)
        {
            switch (character)
            {
                case '\\': escaped.Append("\\\\"); break;
                case '"': escaped.Append("\\\""); break;
                case '\r': escaped.Append("\\r"); break;
                case '\n': escaped.Append("\\n"); break;
                case '\t': escaped.Append("\\t"); break;
                default:
                    if (character < 0x20)
                    {
                        escaped.Append("\\u" + ((int)character).ToString("x4"));
                    }
                    else
                    {
                        escaped.Append(character);
                    }
                    break;
            }
        }
        return escaped.ToString();
    }

    private static string Sha256(string path)
    {
        using (SHA256 algorithm = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
        {
            byte[] digest = algorithm.ComputeHash(stream);
            StringBuilder result = new StringBuilder();
            foreach (byte value in digest)
            {
                result.Append(value.ToString("x2"));
            }
            return result.ToString();
        }
    }

    private static string IdentityJson(ProcessIdentity identity)
    {
        if (identity == null)
        {
            return "null";
        }
        return "{\"pid\":" + identity.Pid
            + ",\"start_time_utc_ticks\":" + identity.StartTimeUtcTicks
            + ",\"name\":\"" + JsonEscape(identity.Name) + "\"}";
    }

    private static void WriteResult(
        string path,
        string revision,
        string specExe,
        string specId,
        string status,
        string expectedMarker,
        string transcriptPath,
        uint childSession,
        uint exitCode,
        int ownedRemaining,
        int providerRemaining,
        bool rootCreatedSuspended,
        bool jobAssignedBeforeResume,
        bool rootResumed
    )
    {
        string json = "{\n"
            + "  \"status\": \"passed\",\n"
            + "  \"source_revision\": \"" + JsonEscape(revision) + "\",\n"
            + "  \"platform\": \"win32\",\n"
            + "  \"pseudoconsole\": \"ConPTY\",\n"
            + "  \"installed_artifact\": true,\n"
            + "  \"spec_executable\": \"" + JsonEscape(Path.GetFullPath(specExe)) + "\",\n"
            + "  \"launch_boundary\": \"venv-python--isolated-module\",\n"
            + "  \"interactive_desktop\": true,\n"
            + "  \"session_id\": " + childSession + ",\n"
            + "  \"terminal_columns\": 180,\n"
            + "  \"terminal_rows\": 50,\n"
            + "  \"dashboard_observed\": true,\n"
            + "  \"selected_spec\": \"" + JsonEscape(specId) + "\",\n"
            + "  \"live_status_observed\": \"" + JsonEscape(status) + "\",\n"
            + "  \"detail_observed\": true,\n"
            + "  \"chat_screen_observed\": true,\n"
            + "  \"chat_provider\": \"codex\",\n"
            + "  \"codex_provider_process_observed\": true,\n"
            + "  \"provider_identity\": " + IdentityJson(ProviderIdentity) + ",\n"
            + "  \"expected_marker\": \"" + JsonEscape(expectedMarker) + "\",\n"
            + "  \"observed_marker\": \"" + JsonEscape(expectedMarker) + "\",\n"
            + "  \"marker_matched\": true,\n"
            + "  \"quit_key\": \"q\",\n"
            + "  \"root_exit_code\": " + exitCode + ",\n"
            + "  \"root_created_suspended\": "
            + (rootCreatedSuspended ? "true" : "false") + ",\n"
            + "  \"job_assigned_before_resume\": "
            + (jobAssignedBeforeResume ? "true" : "false") + ",\n"
            + "  \"root_resumed\": " + (rootResumed ? "true" : "false") + ",\n"
            + "  \"graceful_cleanup_observed\": true,\n"
            + "  \"graceful_owned_processes_remaining\": " + ownedRemaining + ",\n"
            + "  \"emergency_cleanup_invoked\": false,\n"
            + "  \"provider_processes_remaining\": " + providerRemaining + ",\n"
            // The watch app has no dispatcher child. Reuse the exhaustive
            // owned-descendant audit rather than asserting a synthetic zero.
            + "  \"dispatcher_processes_remaining\": " + ownedRemaining + ",\n"
            + "  \"owned_processes_remaining\": " + ownedRemaining + ",\n"
            + "  \"observed_descendant_count\": " + Observed.Count + ",\n"
            + "  \"transcript_file\": \"" + JsonEscape(Path.GetFileName(transcriptPath)) + "\",\n"
            + "  \"transcript_sha256\": \"" + Sha256(transcriptPath) + "\"\n"
            + "}\n";
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, json, new UTF8Encoding(false));
        if (File.Exists(path))
        {
            File.Delete(path);
        }
        File.Move(temporary, path);
    }

    private static void WriteFailureResult(
        string path,
        string revision,
        Exception failure,
        bool emergencyCleanupInvoked,
        int ownedBeforeEmergencyCleanup,
        int ownedAfterEmergencyCleanup,
        string aliveAfterEmergencyCleanup,
        bool rootRemaining,
        bool rootCreatedSuspended,
        bool jobAssignedBeforeResume,
        bool rootResumed
    )
    {
        string json = "{\n"
            + "  \"status\": \"failed\",\n"
            + "  \"source_revision\": \"" + JsonEscape(revision) + "\",\n"
            + "  \"failure\": \"" + JsonEscape(failure.Message) + "\",\n"
            + "  \"root_created_suspended\": "
            + (rootCreatedSuspended ? "true" : "false") + ",\n"
            + "  \"job_assigned_before_resume\": "
            + (jobAssignedBeforeResume ? "true" : "false") + ",\n"
            + "  \"root_resumed\": " + (rootResumed ? "true" : "false") + ",\n"
            + "  \"emergency_cleanup_invoked\": "
            + (emergencyCleanupInvoked ? "true" : "false") + ",\n"
            + "  \"owned_processes_before_emergency_cleanup\": "
            + ownedBeforeEmergencyCleanup + ",\n"
            + "  \"owned_processes_after_emergency_cleanup\": "
            + ownedAfterEmergencyCleanup + ",\n"
            + "  \"alive_after_emergency_cleanup\": \""
            + JsonEscape(aliveAfterEmergencyCleanup) + "\",\n"
            + "  \"root_process_remaining\": "
            + (rootRemaining ? "true" : "false") + "\n"
            + "}\n";
        File.WriteAllText(path, json, new UTF8Encoding(false));
    }

    private static void ConfigureKillOnCloseJob(IntPtr job)
    {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION information =
            new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr pointer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(information, pointer, false);
            RequireWin32(
                SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    pointer,
                    (uint)size
                ),
                "SetInformationJobObject"
            );
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
    }

    public static int Main(string[] args)
    {
        if (args.Length != 6)
        {
            Console.Error.WriteLine(
                "usage: watch-conpty.exe SPEC_EXE REPO_ROOT EVIDENCE_ROOT REVISION SPEC_ID NONCE"
            );
            return 2;
        }

        string specExe = Path.GetFullPath(args[0]);
        string repoRoot = Path.GetFullPath(args[1]);
        string evidenceRoot = Path.GetFullPath(args[2]);
        string revision = args[3].ToLowerInvariant();
        string specId = args[4];
        string nonce = args[5];
        string transcriptPath = Path.Combine(evidenceRoot, "watch-interactive-transcript.log");
        string failedTranscriptPath = Path.Combine(
            evidenceRoot,
            "watch-interactive-failed-transcript.log"
        );
        string failureResultPath = Path.Combine(
            evidenceRoot,
            "watch-interactive-failure.json"
        );
        string resultPath = Path.Combine(evidenceRoot, "watch-interactive-result.json");
        string expectedMarker = "SPEC_WATCH_CODEX_" + nonce;

        SafeFileHandle pseudoInput = null;
        SafeFileHandle hostInput = null;
        SafeFileHandle hostOutput = null;
        SafeFileHandle pseudoOutput = null;
        IntPtr pseudoConsole = IntPtr.Zero;
        IntPtr attributeList = IntPtr.Zero;
        IntPtr job = IntPtr.Zero;
        PROCESS_INFORMATION processInformation = new PROCESS_INFORMATION();
        StreamWriter inputWriter = null;
        StreamReader outputReader = null;
        Thread readerThread = null;
        bool rootCreatedSuspended = false;
        bool jobAssignedBeforeResume = false;
        bool rootResumed = false;

        try
        {
            Require(File.Exists(specExe), "installed spec executable does not exist: " + specExe);
            Require(Directory.Exists(repoRoot), "fixture repository does not exist: " + repoRoot);
            Require(
                revision.Length == 40 && IsLowerHex(revision),
                "source revision must be an exact 40-character SHA"
            );
            Require(
                nonce.Length >= 12 && nonce.Length <= 64 && IsLowerHex(nonce),
                "marker nonce must be 12 to 64 lowercase hexadecimal characters"
            );
            Directory.CreateDirectory(evidenceRoot);
            if (File.Exists(resultPath))
            {
                File.Delete(resultPath);
            }
            if (File.Exists(failedTranscriptPath))
            {
                File.Delete(failedTranscriptPath);
            }
            if (File.Exists(failureResultPath))
            {
                File.Delete(failureResultPath);
            }

            RequireWin32(CreatePipe(out pseudoInput, out hostInput, IntPtr.Zero, 0), "CreatePipe(input)");
            RequireWin32(CreatePipe(out hostOutput, out pseudoOutput, IntPtr.Zero, 0), "CreatePipe(output)");
            COORD size = new COORD { X = 180, Y = 50 };
            int hresult = CreatePseudoConsole(size, pseudoInput, pseudoOutput, 0, out pseudoConsole);
            Require(hresult == 0, "CreatePseudoConsole failed with HRESULT 0x" + hresult.ToString("x8"));

            IntPtr attributeBytes = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref attributeBytes);
            Require(attributeBytes != IntPtr.Zero, "pseudoconsole attribute size was zero");
            attributeList = Marshal.AllocHGlobal(attributeBytes);
            RequireWin32(
                InitializeProcThreadAttributeList(attributeList, 1, 0, ref attributeBytes),
                "InitializeProcThreadAttributeList"
            );
            RequireWin32(
                UpdateProcThreadAttribute(
                    attributeList,
                    0,
                    new IntPtr(PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE),
                    pseudoConsole,
                    new IntPtr(IntPtr.Size),
                    IntPtr.Zero,
                    IntPtr.Zero
                ),
                "UpdateProcThreadAttribute(ConPTY)"
            );

            STARTUPINFOEX startup = new STARTUPINFOEX();
            startup.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
            // Scheduled proof output is itself captured by PowerShell. Windows
            // otherwise passes those redirected parent handles to console
            // children even when bInheritHandles is false, which makes Python
            // report isatty() == false. Null STARTF handles let ConPTY supply
            // the child's real pseudoconsole endpoints (the node-pty pattern).
            startup.StartupInfo.dwFlags |= STARTF_USESTDHANDLES;
            startup.StartupInfo.hStdInput = IntPtr.Zero;
            startup.StartupInfo.hStdOutput = IntPtr.Zero;
            startup.StartupInfo.hStdError = IntPtr.Zero;
            startup.lpAttributeList = attributeList;
            int securityAttributeSize = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
            SECURITY_ATTRIBUTES processSecurity = new SECURITY_ATTRIBUTES
            {
                nLength = securityAttributeSize
            };
            SECURITY_ATTRIBUTES threadSecurity = new SECURITY_ATTRIBUTES
            {
                nLength = securityAttributeSize
            };
            // Establish the ownership boundary before there is any child to
            // escape it. The root starts suspended, is assigned to this
            // kill-on-close Job, and only then may execute user/package code.
            job = CreateJobObject(IntPtr.Zero, null);
            RequireWin32(job != IntPtr.Zero, "CreateJobObject");
            ConfigureKillOnCloseJob(job);
            string commandLine = BuildCommandLine(specExe, repoRoot);
            RequireWin32(
                CreateProcess(
                    null,
                    commandLine,
                    ref processSecurity,
                    ref threadSecurity,
                    false,
                    EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED,
                    IntPtr.Zero,
                    repoRoot,
                    ref startup,
                    out processInformation
                ),
                "CreateProcess(spec watch)"
            );
            rootCreatedSuspended = true;
            RootPid = (int)processInformation.dwProcessId;
            RequireWin32(
                AssignProcessToJobObject(job, processInformation.hProcess),
                "AssignProcessToJobObject"
            );
            jobAssignedBeforeResume = true;
            uint resumeResult = ResumeThread(processInformation.hThread);
            RequireWin32(resumeResult != INVALID_RESUME_RESULT, "ResumeThread(spec watch)");
            rootResumed = true;
            CloseHandle(processInformation.hThread);
            processInformation.hThread = IntPtr.Zero;
            // The pseudoconsole keeps these pipe ends only after its first
            // client process is attached. Closing them before CreateProcess
            // can silently leave the child with ordinary redirected streams,
            // which makes Python report isatty() == false.
            pseudoInput.Dispose();
            pseudoInput = null;
            pseudoOutput.Dispose();
            pseudoOutput = null;

            uint childSession;
            RequireWin32(
                ProcessIdToSessionId(processInformation.dwProcessId, out childSession),
                "ProcessIdToSessionId"
            );
            uint harnessSession;
            RequireWin32(
                ProcessIdToSessionId((uint)Process.GetCurrentProcess().Id, out harnessSession),
                "ProcessIdToSessionId(harness)"
            );
            Require(childSession > 0 && childSession == harnessSession,
                "spec watch did not start in the interactive desktop session");

            outputReader = new StreamReader(
                new FileStream(hostOutput, FileAccess.Read),
                new UTF8Encoding(false, false),
                false,
                4096
            );
            hostOutput = null;
            inputWriter = new StreamWriter(
                new FileStream(hostInput, FileAccess.Write),
                new UTF8Encoding(false, false),
                4096
            );
            hostInput = null;
            inputWriter.AutoFlush = true;
            readerThread = new Thread(delegate() { ReadOutput(outputReader); });
            readerThread.IsBackground = true;
            readerThread.Start();

            int dashboardOffset = OutputLength();
            Require(
                WaitForAll(dashboardOffset, 45, "Specs", "Queue", specId),
                "interactive Textual dashboard did not render the expected run"
            );
            string status = FirstStatus(dashboardOffset);
            Require(status.Length > 0, "dashboard did not render a live run status");

            int detailOffset = OutputLength();
            inputWriter.Write("\r");
            Require(
                WaitForAll(detailOffset, 15, specId, "phase=", "attempts=", "branch="),
                "selected run detail did not render"
            );

            int chatOffset = OutputLength();
            inputWriter.Write("c");
            Require(
                WaitForAll(chatOffset, 15, specId + " chat", "agent=codex", "Ask for run status"),
                "per-spec Codex chat screen did not render"
            );

            string prompt = "Reply with exactly the concatenation SPEC_WATCH_CODEX, "
                + "then one underscore, then " + nonce
                + ". Do not issue a command and do not add any other text.";
            int responseOffset = OutputLength();
            inputWriter.Write(prompt + "\r");
            Require(
                WaitForAll(responseOffset, 180, expectedMarker),
                "real Codex TUI chat did not return the expected marker"
            );
            Require(ProviderObserved, "no Codex provider descendant was observed");
            Require(
                WaitForProviderExit(30),
                "Codex provider descendant remained live after its response"
            );

            int detailReturnOffset = OutputLength();
            inputWriter.Write("\x1b");
            Require(
                WaitForAll(detailReturnOffset, 15, specId, "phase=", "branch="),
                "Escape did not return from chat to detail"
            );
            int dashboardReturnOffset = OutputLength();
            inputWriter.Write("\x1b");
            Require(
                WaitForAll(dashboardReturnOffset, 15, "Specs", "Queue", specId),
                "Escape did not return from detail to dashboard"
            );
            inputWriter.Write("q");

            uint wait = WaitForSingleObject(processInformation.hProcess, 30000);
            Require(wait == WAIT_OBJECT_0, "spec watch did not exit after q");
            uint exitCode;
            RequireWin32(
                GetExitCodeProcess(processInformation.hProcess, out exitCode),
                "GetExitCodeProcess"
            );
            Require(exitCode == 0, "spec watch exited with code " + exitCode);

            // q must end the complete app tree without help from any terminal
            // or ownership teardown. Keep input, ConPTY, and the kill-on-close
            // Job open throughout the descendant audit so none of those
            // closures can turn leaked children into an apparent clean result.
            TrackDescendants();
            int remaining = WaitForObservedExit(15, true);
            int providerRemaining = CountAliveProviders();
            Require(
                remaining == 0,
                remaining + " owned descendant process(es) survived graceful q cleanup: "
                    + DescribeAlive(Observed.Values)
            );
            Require(providerRemaining == 0, "Codex provider process survived q");

            // The exact tree is already empty. Terminal teardown may now close
            // its input and ConPTY before draining the reader; only then is the
            // independently empty Job handle released.
            inputWriter.Dispose();
            inputWriter = null;
            ClosePseudoConsole(pseudoConsole);
            pseudoConsole = IntPtr.Zero;
            if (readerThread != null)
            {
                Require(
                    readerThread.Join(5000),
                    "ConPTY output reader did not finish after graceful q cleanup"
                );
            }

            // All exact descendants are already gone. Releasing the now-empty
            // Job cannot conceal a leak and is not emergency cleanup.
            RequireWin32(CloseHandle(job), "CloseHandle(empty watch Job)");
            job = IntPtr.Zero;

            string transcript = CapturedOutput();
            File.WriteAllText(transcriptPath, transcript, new UTF8Encoding(false));
            Require(
                transcript.IndexOf(expectedMarker, StringComparison.Ordinal) >= 0,
                "retained transcript omitted the observed Codex marker"
            );
            WriteResult(
                resultPath,
                revision,
                specExe,
                specId,
                status,
                expectedMarker,
                transcriptPath,
                childSession,
                exitCode,
                remaining,
                providerRemaining,
                rootCreatedSuspended,
                jobAssignedBeforeResume,
                rootResumed
            );
            Console.WriteLine("interactive ConPTY spec watch proof passed: " + expectedMarker);
            return 0;
        }
        catch (Exception exception)
        {
            int beforeEmergencyCleanup = -1;
            int afterEmergencyCleanup = -1;
            try
            {
                TrackDescendants();
                beforeEmergencyCleanup = CountAlive(Observed.Values);
            }
            catch (Exception processInspectionException)
            {
                Console.Error.WriteLine(
                    "could not inspect descendants before emergency cleanup: "
                        + processInspectionException.Message
                );
            }
            bool emergencyCleanupInvoked = job != IntPtr.Zero
                || ProcessIsRunning(processInformation.hProcess);
            // Exceptional teardown deliberately kills the Job/process before
            // touching ConPTY; the output reader continues draining meanwhile.
            EmergencyCleanup(
                ref job,
                processInformation.hProcess,
                ref pseudoConsole
            );
            if (inputWriter != null)
            {
                inputWriter.Dispose();
                inputWriter = null;
            }
            if (readerThread != null)
            {
                readerThread.Join(5000);
            }
            if (outputReader != null)
            {
                outputReader.Dispose();
                outputReader = null;
                if (readerThread != null && readerThread.IsAlive)
                {
                    readerThread.Join(5000);
                }
            }
            try
            {
                Directory.CreateDirectory(evidenceRoot);
                afterEmergencyCleanup = WaitForObservedExit(10, false);
                File.WriteAllText(
                    failedTranscriptPath,
                    CapturedOutput(),
                    new UTF8Encoding(false)
                );
                WriteFailureResult(
                    failureResultPath,
                    revision,
                    exception,
                    emergencyCleanupInvoked,
                    beforeEmergencyCleanup,
                    afterEmergencyCleanup,
                    DescribeAlive(Observed.Values),
                    ProcessIsRunning(processInformation.hProcess),
                    rootCreatedSuspended,
                    jobAssignedBeforeResume,
                    rootResumed
                );
            }
            catch (Exception transcriptException)
            {
                Console.Error.WriteLine(
                    "could not retain failed ConPTY transcript: " + transcriptException.Message
                );
            }
            Console.Error.WriteLine("interactive ConPTY spec watch proof failed: " + exception);
            return 1;
        }
        finally
        {
            // This path is idempotent with catch. Most importantly, Job/root
            // termination always precedes ConPTY closure on every exception.
            if (job != IntPtr.Zero
                || ProcessIsRunning(processInformation.hProcess)
                || pseudoConsole != IntPtr.Zero)
            {
                EmergencyCleanup(
                    ref job,
                    processInformation.hProcess,
                    ref pseudoConsole
                );
            }
            if (inputWriter != null)
            {
                inputWriter.Dispose();
            }
            if (readerThread != null && readerThread.IsAlive)
            {
                readerThread.Join(5000);
            }
            if (outputReader != null)
            {
                outputReader.Dispose();
            }
            if (hostInput != null)
            {
                hostInput.Dispose();
            }
            if (hostOutput != null)
            {
                hostOutput.Dispose();
            }
            if (pseudoInput != null)
            {
                pseudoInput.Dispose();
            }
            if (pseudoOutput != null)
            {
                pseudoOutput.Dispose();
            }
            if (processInformation.hThread != IntPtr.Zero)
            {
                CloseHandle(processInformation.hThread);
            }
            if (processInformation.hProcess != IntPtr.Zero)
            {
                CloseHandle(processInformation.hProcess);
            }
            if (attributeList != IntPtr.Zero)
            {
                DeleteProcThreadAttributeList(attributeList);
                Marshal.FreeHGlobal(attributeList);
            }
        }
    }
}
