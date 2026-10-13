/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (C) 2026 The ButterEye contributors
 *
 * ButterEye.app/Contents/MacOS/ButterEye: the app's main executable
 * (packaging/macos/build-app.sh compiles it; docs/PLATFORMS.md, "The .app").
 *
 * It embeds the bundled Python (Contents/Resources/python, python-build-standalone's
 * libpython3.14.dylib) rather than exec-ing its python3.14, for one reason that a
 * shell script cannot get round: macOS decides which app a process is -- its name
 * in the menu bar and the Dock, its icon, the bundle Qt and LaunchServices read --
 * from the path of the main executable, and only Contents/MacOS/<CFBundleExecutable>
 * is that path. A script that exec-ed Resources/python/bin/python3.14 would turn
 * ButterEye into a process called "python3.14" with no bundle.
 *
 * One binary, two programs:
 *   - started with no arguments (Finder, the Dock, `open ButterEye.app`) it runs
 *     the window: `python -m buttereye.gui`;
 *   - started with arguments, or under the name `buttereye`, it is the command
 *     line: `python -m buttereye.cli ARGS`. The name matters because APFS and HFS+
 *     are case-insensitive by default, so Contents/MacOS/buttereye *is* this file
 *     and `.../Contents/MacOS/buttereye doctor` is the documented way in; a second
 *     file of that name could not exist beside this one.
 * LaunchServices may add one "-psn_0_NNNN" argument (an old process serial number);
 * that alone does not make a command line.
 *
 * The interpreter is isolated, like `python -I`: no PYTHON* variable, no user
 * site-packages and no current directory on sys.path, so nothing outside the
 * bundle chooses what code ButterEye runs. It writes no bytecode either: the
 * bundle is signed, and a __pycache__ written into it after signing would break
 * the seal (and /Applications may not be writable at all). The build ships
 * precompiled unchecked-hash .pyc files instead.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef PY_EXE
#error "build-app.sh defines PY_EXE (e.g. \"python3.14\")"
#endif

static void die(const char *what) {
    fprintf(stderr, "ButterEye: %s\n", what);
    exit(70); /* EX_SOFTWARE */
}

/* "<dir>/<a>/<b>..." with every part checked against PATH_MAX. */
static void join(char *out, const char *base, const char *tail) {
    if (snprintf(out, PATH_MAX, "%s/%s", base, tail) >= PATH_MAX)
        die("the app's path is too long");
}

static int is_cli(int argc, char **argv) {
    const char *base = strrchr(argv[0], '/');
    base = base ? base + 1 : argv[0];
    if (strcmp(base, "buttereye") == 0)
        return 1;
    for (int i = 1; i < argc; i++)
        if (strncmp(argv[i], "-psn_", 5) != 0)
            return 1;
    return 0;
}

static void check(PyStatus status, PyConfig *config) {
    if (PyStatus_Exception(status)) {
        PyConfig_Clear(config);
        Py_ExitStatusException(status);
    }
}

int main(int argc, char **argv) {
    char raw[PATH_MAX], exe[PATH_MAX], contents[PATH_MAX];
    char home[PATH_MAX], python[PATH_MAX];
    uint32_t size = sizeof raw;

    if (_NSGetExecutablePath(raw, &size) != 0 || realpath(raw, exe) == NULL)
        die("cannot find where the app is");
    /* exe = <...>/ButterEye.app/Contents/MacOS/ButterEye: two levels up */
    strcpy(contents, exe);
    for (int up = 0; up < 2; up++) {
        char *slash = strrchr(contents, '/');
        if (slash == NULL || slash == contents)
            die("the launcher is not inside an app bundle");
        *slash = '\0';
    }
    join(home, contents, "Resources/python");
    join(python, home, "bin/" PY_EXE);

    int cli = is_cli(argc, argv);
    /* the GUI gets no arguments: drop LaunchServices' -psn_ one */
    int pargc = cli ? argc : 1;

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.isolated = 1;          /* -I: implies the three below */
    config.use_environment = 0;   /* -E */
    config.user_site_directory = 0; /* -s */
    config.safe_path = 1;         /* -P */
    config.write_bytecode = 0;    /* -B: never write into the signed bundle */
    config.parse_argv = 0;        /* ButterEye's arguments are ButterEye's */
    check(PyConfig_SetBytesString(&config, &config.home, home), &config);
    /* sys.executable: the real interpreter, so anything that runs
       `sys.executable -m ...` gets this same Python */
    check(PyConfig_SetBytesString(&config, &config.executable, python), &config);
    check(PyConfig_SetBytesString(&config, &config.program_name, python), &config);
    check(PyConfig_SetString(&config, &config.run_module,
                             cli ? L"buttereye.cli" : L"buttereye.gui"), &config);
    check(PyConfig_SetBytesArgv(&config, pargc, argv), &config);
    check(Py_InitializeFromConfig(&config), &config);
    PyConfig_Clear(&config);
    return Py_RunMain();
}
