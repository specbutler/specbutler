using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;

// A deterministic provider double for the native autopilot process proof.
// The release proof uses the real Codex binary for lifecycle and web chat;
// this executable isolates dispatcher adoption from provider latency so a
// child can be held across an intentional dispatcher crash and released on
// demand. It is compiled inside the disposable guest with Windows' csc.exe.
public static class AutopilotProofAgent
{
    private static string JsonEscape(string value)
    {
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"")
            .Replace("\r", "\\r").Replace("\n", "\\n");
    }

    public static int Main(string[] args)
    {
        if (args.Contains("--help"))
        {
            Console.WriteLine("proof codex exec --json --output-schema");
            return 0;
        }

        string eventRoot = Environment.GetEnvironmentVariable("SPEC_AUTOPILOT_PROOF_EVENTS") ?? "";
        string releasePath = Environment.GetEnvironmentVariable("SPEC_AUTOPILOT_PROOF_RELEASE") ?? "";
        string specExe = Environment.GetEnvironmentVariable("SPEC_AUTOPILOT_SPEC_EXE") ?? "spec.exe";
        string specId = Environment.GetEnvironmentVariable("SPEC_ID") ?? "unknown";
        string runId = Environment.GetEnvironmentVariable("SPEC_RUN_ID") ?? "unknown";
        if (eventRoot.Length == 0 || releasePath.Length == 0)
        {
            Console.Error.WriteLine("autopilot proof environment is incomplete");
            return 2;
        }

        Directory.CreateDirectory(eventRoot);
        int pid = Process.GetCurrentProcess().Id;
        string startedPath = Path.Combine(eventRoot, "started-" + pid + ".json");
        File.WriteAllText(
            startedPath,
            "{\"event\":\"agent-started\",\"pid\":" + pid
            + ",\"spec_id\":\"" + JsonEscape(specId)
            + "\",\"run_id\":\"" + JsonEscape(runId) + "\"}\n"
        );

        DateTime deadline = DateTime.UtcNow.AddMinutes(5);
        while (!File.Exists(releasePath) && DateTime.UtcNow < deadline)
        {
            Thread.Sleep(100);
        }
        if (!File.Exists(releasePath))
        {
            Console.Error.WriteLine("autopilot proof release marker timed out");
            return 3;
        }

        ProcessStartInfo report = new ProcessStartInfo();
        report.FileName = specExe;
        report.Arguments = "report --status needs-input --summary \"intentional autopilot adoption proof hold\"";
        report.UseShellExecute = false;
        Process child = Process.Start(report);
        child.WaitForExit();
        File.WriteAllText(
            Path.Combine(eventRoot, "finished-" + pid + ".json"),
            "{\"event\":\"agent-finished\",\"pid\":" + pid
            + ",\"spec_id\":\"" + JsonEscape(specId)
            + "\",\"run_id\":\"" + JsonEscape(runId)
            + "\",\"report_exit_code\":" + child.ExitCode + "}\n"
        );
        return child.ExitCode;
    }
}
