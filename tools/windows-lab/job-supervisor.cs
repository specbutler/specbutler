using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// Launches the complete interactive proof inside a kill-on-close Windows Job.
// The child is created suspended and assigned before its first instruction, so
// stopping the scheduled-task runner cannot strand provider, Git, or test
// descendants. This intentionally targets the C# compiler available to Windows
// PowerShell 5.1.
public static class SpecButlerLabJobSupervisor
{
    private const uint CREATE_SUSPENDED = 0x00000004;
    private const uint CREATE_NO_WINDOW = 0x08000000;
    private const uint STARTF_USESTDHANDLES = 0x00000100;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    private const int JobObjectBasicAccountingInformation = 1;
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 258;
    private const uint INVALID_RESUME_RESULT = 0xffffffff;
    private const uint GENERIC_READ = 0x80000000;
    private const uint GENERIC_WRITE = 0x40000000;
    private const uint FILE_SHARE_READ = 0x00000001;
    private const uint FILE_SHARE_WRITE = 0x00000002;
    private const uint FILE_SHARE_DELETE = 0x00000004;
    private const uint CREATE_ALWAYS = 2;
    private const uint OPEN_EXISTING = 3;
    private const uint FILE_ATTRIBUTE_NORMAL = 0x00000080;
    private static readonly IntPtr InvalidHandleValue = new IntPtr(-1);

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

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    {
        public long TotalUserTime;
        public long TotalKernelTime;
        public long ThisPeriodTotalUserTime;
        public long ThisPeriodTotalKernelTime;
        public uint TotalPageFaultCount;
        public uint TotalProcesses;
        public uint ActiveProcesses;
        public uint TotalTerminatedProcesses;
    }

    public sealed class Result
    {
        public int ExitCode { get; set; }
        public bool TimedOut { get; set; }
        public uint RootProcessId { get; set; }
        public bool DescendantsGone { get; set; }
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateFile(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        ref SECURITY_ATTRIBUTES securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile
    );

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcess(
        string applicationName,
        StringBuilder commandLine,
        ref SECURITY_ATTRIBUTES processAttributes,
        ref SECURITY_ATTRIBUTES threadAttributes,
        bool inheritHandles,
        uint creationFlags,
        IntPtr environment,
        string currentDirectory,
        ref STARTUPINFO startupInfo,
        out PROCESS_INFORMATION processInformation
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr jobAttributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        IntPtr information,
        uint informationLength
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool QueryInformationJobObject(
        IntPtr job,
        int informationClass,
        IntPtr information,
        uint informationLength,
        out uint returnLength
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    private static void RequireWin32(bool condition, string operation)
    {
        if (!condition)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), operation);
        }
    }

    private static string QuoteArgument(string value)
    {
        if (value == null)
        {
            throw new ArgumentNullException("value");
        }
        if (value.Length > 0 && value.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
        {
            return value;
        }
        var result = new StringBuilder();
        result.Append('"');
        var backslashes = 0;
        foreach (var character in value)
        {
            if (character == '\\')
            {
                backslashes += 1;
                continue;
            }
            if (character == '"')
            {
                result.Append('\\', (backslashes * 2) + 1);
                result.Append('"');
                backslashes = 0;
                continue;
            }
            result.Append('\\', backslashes);
            backslashes = 0;
            result.Append(character);
        }
        result.Append('\\', backslashes * 2);
        result.Append('"');
        return result.ToString();
    }

    private static StringBuilder BuildCommandLine(string application, string[] arguments)
    {
        var parts = new List<string>();
        parts.Add(QuoteArgument(application));
        if (arguments != null)
        {
            foreach (var argument in arguments)
            {
                parts.Add(QuoteArgument(argument));
            }
        }
        return new StringBuilder(string.Join(" ", parts.ToArray()));
    }

    private static void ConfigureKillOnClose(IntPtr job)
    {
        var information = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        var size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        var pointer = Marshal.AllocHGlobal(size);
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

    private static uint GetActiveProcessCount(IntPtr job)
    {
        var size = Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
        var pointer = Marshal.AllocHGlobal(size);
        try
        {
            uint returned;
            RequireWin32(
                QueryInformationJobObject(
                    job,
                    JobObjectBasicAccountingInformation,
                    pointer,
                    (uint)size,
                    out returned
                ),
                "QueryInformationJobObject"
            );
            var information = (JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                Marshal.PtrToStructure(
                    pointer,
                    typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                );
            return information.ActiveProcesses;
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
    }

    private static bool WaitForJobEmpty(IntPtr job, int timeoutMilliseconds)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMilliseconds);
        do
        {
            if (GetActiveProcessCount(job) == 0)
            {
                return true;
            }
            Thread.Sleep(50);
        }
        while (DateTime.UtcNow < deadline);
        return GetActiveProcessCount(job) == 0;
    }

    public static Result Run(
        string application,
        string[] arguments,
        string currentDirectory,
        string logPath,
        int timeoutMilliseconds
    )
    {
        if (string.IsNullOrEmpty(application))
        {
            throw new ArgumentException("Application is required.", "application");
        }
        if (string.IsNullOrEmpty(currentDirectory))
        {
            throw new ArgumentException("Working directory is required.", "currentDirectory");
        }
        if (string.IsNullOrEmpty(logPath))
        {
            throw new ArgumentException("Log path is required.", "logPath");
        }
        if (timeoutMilliseconds <= 0)
        {
            throw new ArgumentOutOfRangeException("timeoutMilliseconds");
        }

        var inheritable = new SECURITY_ATTRIBUTES
        {
            nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)),
            lpSecurityDescriptor = IntPtr.Zero,
            bInheritHandle = 1,
        };
        var processAttributes = new SECURITY_ATTRIBUTES
        {
            nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)),
            lpSecurityDescriptor = IntPtr.Zero,
            bInheritHandle = 0,
        };
        var threadAttributes = processAttributes;
        var logHandle = IntPtr.Zero;
        var inputHandle = IntPtr.Zero;
        var job = IntPtr.Zero;
        var processInformation = new PROCESS_INFORMATION();
        var created = false;
        var assigned = false;
        try
        {
            logHandle = CreateFile(
                logPath,
                GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                ref inheritable,
                CREATE_ALWAYS,
                FILE_ATTRIBUTE_NORMAL,
                IntPtr.Zero
            );
            RequireWin32(logHandle != InvalidHandleValue, "CreateFile(log)");
            inputHandle = CreateFile(
                "NUL",
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                ref inheritable,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                IntPtr.Zero
            );
            RequireWin32(inputHandle != InvalidHandleValue, "CreateFile(NUL)");

            job = CreateJobObject(IntPtr.Zero, null);
            RequireWin32(job != IntPtr.Zero, "CreateJobObject");
            ConfigureKillOnClose(job);

            var startupInfo = new STARTUPINFO
            {
                cb = Marshal.SizeOf(typeof(STARTUPINFO)),
                dwFlags = (int)STARTF_USESTDHANDLES,
                hStdInput = inputHandle,
                hStdOutput = logHandle,
                hStdError = logHandle,
            };
            RequireWin32(
                CreateProcess(
                    application,
                    BuildCommandLine(application, arguments),
                    ref processAttributes,
                    ref threadAttributes,
                    true,
                    CREATE_SUSPENDED | CREATE_NO_WINDOW,
                    IntPtr.Zero,
                    currentDirectory,
                    ref startupInfo,
                    out processInformation
                ),
                "CreateProcess"
            );
            created = true;
            RequireWin32(
                AssignProcessToJobObject(job, processInformation.hProcess),
                "AssignProcessToJobObject"
            );
            assigned = true;
            RequireWin32(
                ResumeThread(processInformation.hThread) != INVALID_RESUME_RESULT,
                "ResumeThread"
            );
            CloseHandle(processInformation.hThread);
            processInformation.hThread = IntPtr.Zero;

            var wait = WaitForSingleObject(
                processInformation.hProcess,
                (uint)timeoutMilliseconds
            );
            if (wait == WAIT_TIMEOUT)
            {
                RequireWin32(TerminateJobObject(job, 124), "TerminateJobObject(timeout)");
                if (!WaitForJobEmpty(job, 30000))
                {
                    throw new InvalidOperationException(
                        "Timed-out interactive proof Job retained processes after termination."
                    );
                }
                return new Result
                {
                    ExitCode = 124,
                    TimedOut = true,
                    RootProcessId = processInformation.dwProcessId,
                    DescendantsGone = true,
                };
            }
            if (wait != WAIT_OBJECT_0)
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "WaitForSingleObject"
                );
            }
            uint exitCode;
            RequireWin32(
                GetExitCodeProcess(processInformation.hProcess, out exitCode),
                "GetExitCodeProcess"
            );
            if (!WaitForJobEmpty(job, 30000))
            {
                RequireWin32(
                    TerminateJobObject(job, 125),
                    "TerminateJobObject(descendant cleanup)"
                );
                if (!WaitForJobEmpty(job, 30000))
                {
                    throw new InvalidOperationException(
                        "Interactive proof Job retained descendants after forced cleanup."
                    );
                }
                throw new InvalidOperationException(
                    "Interactive proof root exited while descendant processes remained; " +
                    "the Job terminated them."
                );
            }
            return new Result
            {
                ExitCode = unchecked((int)exitCode),
                TimedOut = false,
                RootProcessId = processInformation.dwProcessId,
                DescendantsGone = true,
            };
        }
        finally
        {
            Exception cleanupFailure = null;
            try
            {
                if (
                    created &&
                    !assigned &&
                    processInformation.hProcess != IntPtr.Zero
                )
                {
                    RequireWin32(
                        TerminateProcess(processInformation.hProcess, 125),
                        "TerminateProcess(unassigned suspended child)"
                    );
                    if (
                        WaitForSingleObject(processInformation.hProcess, 30000) !=
                        WAIT_OBJECT_0
                    )
                    {
                        throw new InvalidOperationException(
                            "Unassigned suspended proof child did not terminate."
                        );
                    }
                }
            }
            catch (Exception exception)
            {
                cleanupFailure = exception;
            }
            if (created && processInformation.hThread != IntPtr.Zero)
            {
                CloseHandle(processInformation.hThread);
            }
            if (created && processInformation.hProcess != IntPtr.Zero)
            {
                CloseHandle(processInformation.hProcess);
            }
            if (job != IntPtr.Zero)
            {
                CloseHandle(job);
            }
            if (inputHandle != IntPtr.Zero && inputHandle != InvalidHandleValue)
            {
                CloseHandle(inputHandle);
            }
            if (logHandle != IntPtr.Zero && logHandle != InvalidHandleValue)
            {
                CloseHandle(logHandle);
            }
            if (cleanupFailure != null)
            {
                throw cleanupFailure;
            }
        }
    }
}
