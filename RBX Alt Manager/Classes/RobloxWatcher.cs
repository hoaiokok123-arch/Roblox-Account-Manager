using Microsoft.Win32;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Timers;

namespace RBX_Alt_Manager.Classes
{
    internal class RobloxWatcher
    {
        [DllImport("user32.dll")]
        static extern IntPtr GetForegroundWindow();

        [DllImport("user32.dll", SetLastError = true)]
        static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

        [StructLayout(LayoutKind.Sequential)]
        public struct RECT
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        public static readonly string HandlePath = Path.Combine(Environment.CurrentDirectory, "handle.bin");

        public static HashSet<int> Seen = new HashSet<int>();
        public static List<RobloxProcess> Instances = new List<RobloxProcess>();
        public static bool VerifyDataModel = true;
        public static bool IgnoreExistingProcesses = true;
        public static bool CloseIfMemoryLow = false;
        public static bool CloseIfWindowTitle = false;
        public static bool RememberWindowPositions = false;
        public static bool AutoRejoin = false;
        public static int MemoryLowValue = 200;
        public static int AutoRejoinDelay = 15;
        public static string ExpectedWindowTitle = "Roblox";
        private static readonly object AutoRejoinLock = new object();
        private static readonly Dictionary<string, DateTime> AutoRejoinSuppressedUntil = new Dictionary<string, DateTime>();

        public static Timer ReadTimer
        {
            get
            {
                if (readTimer == null)
                {
                    readTimer = new Timer(250);
                    readTimer.Elapsed += (s, e) => LogFileRead?.Invoke(null, new EventArgs());
                }

                return readTimer;
            }
        }
        private static Timer readTimer;

        public static event EventHandler<EventArgs> LogFileRead;

        public static void SuppressAutoRejoin(string browserTrackerId, TimeSpan duration)
        {
            if (string.IsNullOrEmpty(browserTrackerId)) return;

            lock (AutoRejoinLock)
                AutoRejoinSuppressedUntil[browserTrackerId] = DateTime.Now.Add(duration);
        }

        public static bool IsAutoRejoinSuppressed(string browserTrackerId)
        {
            if (string.IsNullOrEmpty(browserTrackerId)) return false;

            lock (AutoRejoinLock)
            {
                if (!AutoRejoinSuppressedUntil.TryGetValue(browserTrackerId, out DateTime suppressedUntil))
                    return false;

                if (suppressedUntil > DateTime.Now)
                    return true;

                AutoRejoinSuppressedUntil.Remove(browserTrackerId);
                return false;
            }
        }

        public static void QueueAutoRejoin(Account account, string browserTrackerId, string reason)
        {
            if (!AutoRejoin || Program.Closed || account == null || !account.HasLastLaunchTarget)
                return;

            if (IsAutoRejoinSuppressed(browserTrackerId))
            {
                Program.Logger.Info($"Auto rejoin suppressed for {account.Username} ({browserTrackerId})");
                return;
            }

            long placeId = account.LastLaunchPlaceId;
            string jobId = account.LastLaunchJobId;
            bool followUser = account.LastLaunchFollowUser;
            bool joinVip = account.LastLaunchJoinVIP;
            DateTime sourceLaunchTime = account.LastLaunchTime;

            lock (AutoRejoinLock)
            {
                if (account.AutoRejoinPending)
                    return;

                account.AutoRejoinPending = true;
            }

            Task.Run(async () =>
            {
                try
                {
                    int delaySeconds = Math.Max(AutoRejoinDelay, 5);

                    if (account.LastAutoRejoinAttempt != default)
                    {
                        double cooldown = delaySeconds - (DateTime.Now - account.LastAutoRejoinAttempt).TotalSeconds;

                        if (cooldown > delaySeconds)
                            cooldown = delaySeconds;

                        if (cooldown > 0)
                            delaySeconds = Math.Max(delaySeconds, (int)Math.Ceiling(cooldown));
                    }

                    Program.Logger.Info($"Auto rejoin queued for {account.Username} in {delaySeconds} second(s). Reason: {reason}");

                    await Task.Delay(delaySeconds * 1000);

                    while (!Program.Closed && AutoRejoin && !Utilities.IsConnectedToInternet())
                    {
                        Program.Logger.Info($"Auto rejoin waiting for internet before launching {account.Username}");
                        await Task.Delay(5000);
                    }

                    if (Program.Closed || !AutoRejoin)
                        return;

                    if (account.LastLaunchTime != sourceLaunchTime)
                    {
                        Program.Logger.Info($"Auto rejoin skipped for {account.Username}; account was launched again manually");
                        return;
                    }

                    if (HasRunningProcess(browserTrackerId))
                    {
                        Program.Logger.Info($"Auto rejoin skipped for {account.Username}; Roblox is already running");
                        return;
                    }

                    account.LastAutoRejoinAttempt = DateTime.Now;

                    string result = await account.JoinServer(placeId, jobId, followUser, joinVip, true);

                    if (!result.Contains("Success"))
                        Program.Logger.Warn($"Auto rejoin failed for {account.Username}: {result}");
                    else
                        Program.Logger.Info($"Auto rejoin launched {account.Username}");
                }
                catch (Exception x)
                {
                    Program.Logger.Error($"Auto rejoin failed for {account?.Username}: {x}");
                }
                finally
                {
                    account.AutoRejoinPending = false;
                }
            });
        }

        private static bool HasRunningProcess(string browserTrackerId)
        {
            if (string.IsNullOrEmpty(browserTrackerId))
                return false;

            foreach (var process in Process.GetProcessesByName("RobloxPlayerBeta"))
            {
                try
                {
                    if (process.HasExited) continue;

                    string commandLine = process.GetCommandLine();
                    Match trackerMatch = Regex.Match(commandLine ?? string.Empty, @"\-b (\d+)");

                    if (trackerMatch.Success && trackerMatch.Groups[1].Value == browserTrackerId)
                        return true;
                }
                catch { }
            }

            return false;
        }

        public static void CheckProcesses()
        {
            IntPtr Focused = GetForegroundWindow();

            foreach (var process in Process.GetProcessesByName("RobloxPlayerBeta"))
            {
                if (process.MainWindowHandle == Focused) continue; // Entirely ignore focused windows

                void Kill(string Reason) { Program.Logger.Info($"Attempting to kill process {process.Id}, reason: {Reason}"); try { process.Kill(); } catch { } }

                string CommandLine = process.GetCommandLine();

                // This ignores the second roblox process which would cause 268 (Unexpected client behavior) kicks if it were closed.
                if (string.IsNullOrEmpty(CommandLine)) continue; // Roblox's second process
                if (CommandLine.StartsWith("\\??\\")) continue; // Roblox's second process
                if (!CommandLine.Contains("-t ") && !CommandLine.Contains("-j ")) continue; // Check if this process was ran with an authentcation token and a joinScript

                try
                {
                    if ((DateTime.Now - process.StartTime).TotalSeconds > 30) // Roblox shouldn't take that long to startup, right? Surely nobody will be using a potato with these settings.
                    {
                        if (CloseIfMemoryLow && process.WorkingSet64 / 1024 / 1024 < MemoryLowValue)
                            Kill($"Low Memory ({process.WorkingSet64 / 1024 / 1024} < {MemoryLowValue})");

                        if (CloseIfWindowTitle && process.MainWindowTitle != ExpectedWindowTitle)
                            Kill($"Window Title isn't {ExpectedWindowTitle}, got {process.MainWindowTitle}");
                    }
                }
                catch (Exception x) { Program.Logger.Error($"Error with checking for Memory & Window Title: {x.Message}\n{x.StackTrace}"); }

                if (RememberWindowPositions && (DateTime.Now - process.StartTime).TotalSeconds > 30)
                {
                    var TrackerMatch = Regex.Match(CommandLine, @"\-b (\d+)");
                    string TrackerID = TrackerMatch.Success ? TrackerMatch.Groups[1].Value : string.Empty;

                    if (AccountManager.AccountsList.FirstOrDefault(Account => Account.BrowserTrackerID == TrackerID) is Account account)
                        try
                        {
                            GetWindowRect(process.MainWindowHandle, out RECT rect);

                            account.SetField("Window_Position_X", $"{rect.Left:0}");
                            account.SetField("Window_Position_Y", $"{rect.Top:0}");
                            account.SetField("Window_Width", $"{rect.Right - rect.Left:0}");
                            account.SetField("Window_Height", $"{rect.Bottom - rect.Top:0}");
                        }
                        catch { }
                }

                if (Seen.Contains(process.Id)) continue;

                try
                {
                    if (process.HasExited) continue; // Will throw an exception if we have no access, wrapped in a try-catch to ignore Roblox's second process which is ran with elevated permissions

                    Instances.Add(new RobloxProcess(process, CommandLine));
                    Seen.Add(process.Id);
                }
                catch (Exception x) { Program.Logger.Error($"Access to Process {process.Id} denied! This may be due to roblox being ran as admin or roblox's second process(This message can be ignored): {x.Message}"); }
            }
        }

        public static bool IsHandleEulaAccepted()
        {
            RegistryKey AcceptedHEULA = Registry.CurrentUser.OpenSubKey(@"SOFTWARE\Sysinternals\Handle");
            object EulaObject = AcceptedHEULA?.GetValue("EulaAccepted");

            return AcceptedHEULA != null && EulaObject != null && int.TryParse(EulaObject.ToString(), out int EULA) && EULA == 1;
        }
    }
}
