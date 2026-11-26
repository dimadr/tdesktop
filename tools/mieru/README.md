# Telegramita Mieru runtime

Windows builds require the locally supplied Mieru 3.40.0 executable at:

```text
tools/mieru/3.40.0/mieru.exe
```

The executable is intentionally ignored by Git and is copied next to
`Telegram.exe` by CMake. Its expected SHA-256 is:

```text
C120FE9389E034AC2C9A405FAF58E99BF3A7A5EA9075808BE20622FC6494BF0B
```

Do not download or substitute another Mieru version during the build.

Windows builds require -DDESKTOP_APP_DISABLE_AUTOUPDATE=ON so official
updates cannot replace the Mieru integration.
