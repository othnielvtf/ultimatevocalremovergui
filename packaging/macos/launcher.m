/* Native app entry point. Keep Tk and its windows in this app's process. */
#import <AppKit/AppKit.h>
#include <Python.h>
#include <mach-o/dyld.h>
#include <stdlib.h>

static BOOL commandLineCheck(int argc, char *argv[]) {
    for (int index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--self-test") == 0 ||
            strcmp(argv[index], "--smoke-test") == 0) {
            return YES;
        }
    }
    return NO;
}

static int fail(NSString *message, BOOL cli) {
    fprintf(stderr, "UVR Modern Models: %s\n", message.UTF8String);
    if (!cli) {
        /* Do not create NSApplication on the normal path: Tk supplies its own. */
        [NSApplication sharedApplication];
        NSAlert *alert = [[NSAlert alloc] init];
        alert.messageText = @"UVR Modern Models could not start";
        alert.informativeText = [message stringByAppendingString:
            @"\n\nApplication logs are in ~/Library/Logs/UVRModern/app.log."];
        [alert addButtonWithTitle:@"OK"];
        [alert runModal];
    }
    return 1;
}

static NSString *statusMessage(PyStatus status) {
    NSString *message = status.err_msg ?
        [NSString stringWithUTF8String:status.err_msg] : @"Python initialization failed.";
    if (status.func) {
        message = [NSString stringWithFormat:@"%s: %@", status.func, message];
    }
    return message;
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        BOOL cli = commandLineCheck(argc, argv);
        uint32_t length = 0;
        _NSGetExecutablePath(NULL, &length);
        char *buffer = calloc(length, 1);
        if (!buffer || _NSGetExecutablePath(buffer, &length) != 0) {
            free(buffer);
            return fail(@"Could not locate the application bundle.", cli);
        }
        NSString *executable = [[NSString stringWithUTF8String:buffer]
            stringByResolvingSymlinksInPath];
        free(buffer);
        NSString *contents = [[executable stringByDeletingLastPathComponent]
            stringByDeletingLastPathComponent];
        NSString *resources = [contents stringByAppendingPathComponent:@"Resources"];
        NSString *runtime = [resources stringByAppendingPathComponent:@"runtime"];
        NSString *python = [runtime stringByAppendingPathComponent:@"bin/python3.12"];
        NSString *bootstrap = [resources stringByAppendingPathComponent:@"bootstrap.py"];
        if (![[NSFileManager defaultManager] isExecutableFileAtPath:python] ||
            ![[NSFileManager defaultManager] fileExistsAtPath:bootstrap]) {
            return fail(@"The bundled Python runtime or application files are missing. Reinstall the app.", cli);
        }

        PyConfig config;
        PyConfig_InitIsolatedConfig(&config);
        config.write_bytecode = 0;
        config.parse_argv = 0;
        PyStatus status = PyConfig_SetBytesString(&config, &config.home, runtime.fileSystemRepresentation);
        if (!PyStatus_Exception(status)) {
            status = PyConfig_SetBytesString(&config, &config.program_name, python.fileSystemRepresentation);
        }
        if (!PyStatus_Exception(status)) {
            status = PyConfig_SetBytesString(&config, &config.executable, python.fileSystemRepresentation);
        }
        if (!PyStatus_Exception(status)) {
            status = PyConfig_SetBytesString(&config, &config.run_filename, bootstrap.fileSystemRepresentation);
        }
        if (!PyStatus_Exception(status)) {
            status = PyConfig_SetBytesArgv(&config, argc, argv);
        }
        if (!PyStatus_Exception(status)) {
            status = Py_InitializeFromConfig(&config);
        }
        if (PyStatus_Exception(status)) {
            NSString *message = statusMessage(status);
            PyConfig_Clear(&config);
            return fail(message, cli);
        }
        PyConfig_Clear(&config);
        int result = Py_RunMain();
        if (result && !cli) {
            return fail(@"The application exited unexpectedly. Open the application log for details.", NO);
        }
        return result;
    }
}
