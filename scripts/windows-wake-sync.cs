using System;
using System.Globalization;
using System.IO;
using System.Security.Principal;

// A deliberately tiny Windows-side adapter. It accepts only one validated
// epoch and can update only one fixed Task Scheduler entry. Device actions,
// shell payloads and arbitrary task names are not exposed.
internal static class HomeButlerWakeSync
{
    private const string WakeTaskName = "Home Butler Scheduler Wake";
    private const string RuntimeTaskName = "Home Butler WSL Runtime";
    private const int TaskCreateOrUpdate = 6;
    private const int TaskLogonInteractiveToken = 3;
    private const int TaskTriggerTime = 1;
    private const int TaskActionExec = 0;
    private const int TaskInstancesIgnoreNew = 2;

    private static int Main(string[] args)
    {
        long epoch;
        if (args.Length != 1 ||
            !long.TryParse(args[0], NumberStyles.None, CultureInfo.InvariantCulture, out epoch))
        {
            Console.WriteLine("{\"schema_version\":1,\"status\":\"invalid_epoch\"}");
            return 2;
        }

        DateTimeOffset wakeAt;
        try
        {
            wakeAt = DateTimeOffset.FromUnixTimeSeconds(epoch);
        }
        catch (ArgumentOutOfRangeException)
        {
            Console.WriteLine("{\"schema_version\":1,\"status\":\"invalid_epoch\"}");
            return 2;
        }

        DateTimeOffset now = DateTimeOffset.Now;
        if (wakeAt <= now.AddSeconds(30) || wakeAt > now.AddDays(366))
        {
            Console.WriteLine("{\"schema_version\":1,\"status\":\"invalid_epoch\"}");
            return 2;
        }

        try
        {
            Type serviceType = Type.GetTypeFromProgID("Schedule.Service", true);
            dynamic service = Activator.CreateInstance(serviceType);
            service.Connect();
            dynamic root = service.GetFolder("\\");
            dynamic definition = service.NewTask(0);

            definition.RegistrationInfo.Description =
                "Wake Windows for the next task exported by the Home Butler scheduler.";
            definition.Principal.UserId = WindowsIdentity.GetCurrent().User.Value;
            definition.Principal.LogonType = TaskLogonInteractiveToken;
            definition.Principal.RunLevel = 0;

            definition.Settings.Enabled = true;
            definition.Settings.Hidden = true;
            definition.Settings.StartWhenAvailable = true;
            definition.Settings.WakeToRun = true;
            definition.Settings.DisallowStartIfOnBatteries = false;
            definition.Settings.StopIfGoingOnBatteries = false;
            definition.Settings.MultipleInstances = TaskInstancesIgnoreNew;
            definition.Settings.ExecutionTimeLimit = "PT1M";

            dynamic trigger = definition.Triggers.Create(TaskTriggerTime);
            trigger.StartBoundary = wakeAt.LocalDateTime.ToString(
                "yyyy-MM-dd'T'HH:mm:ss", CultureInfo.InvariantCulture);
            trigger.Enabled = true;

            dynamic action = definition.Actions.Create(TaskActionExec);
            action.Path = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System), "schtasks.exe");
            action.Arguments = "/Run /TN \"" + RuntimeTaskName + "\"";

            root.RegisterTaskDefinition(
                WakeTaskName,
                definition,
                TaskCreateOrUpdate,
                null,
                null,
                TaskLogonInteractiveToken,
                null);

            dynamic registered = root.GetTask(WakeTaskName);
            dynamic registeredTrigger = registered.Definition.Triggers.Item(1);
            bool wakeEnabled = registered.Definition.Settings.WakeToRun;
            string actualBoundary = registeredTrigger.StartBoundary as string;
            string expectedBoundary = wakeAt.LocalDateTime.ToString(
                "yyyy-MM-dd'T'HH:mm:ss", CultureInfo.InvariantCulture);
            if (!wakeEnabled || !string.Equals(
                    actualBoundary, expectedBoundary, StringComparison.OrdinalIgnoreCase))
            {
                Console.WriteLine("{\"schema_version\":1,\"status\":\"verification_failed\"}");
                return 3;
            }

            Console.WriteLine(
                "{\"schema_version\":1,\"status\":\"synced\",\"wake_epoch\":" +
                epoch.ToString(CultureInfo.InvariantCulture) +
                ",\"task\":\"Home Butler Scheduler Wake\"}");
            return 0;
        }
        catch
        {
            // COM/identity details may contain private host data; fail closed.
            Console.WriteLine("{\"schema_version\":1,\"status\":\"unavailable\"}");
            return 3;
        }
    }
}
