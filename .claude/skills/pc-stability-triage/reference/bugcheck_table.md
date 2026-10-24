# Bug check and live dump codes

Load this when a stop code shows up and you need its meaning, parameter hint or category.
Sources: the Kernel-Power 41 decimal rule and every name and meaning below were checked on
2026-10-23 against Microsoft Learn:
<https://learn.microsoft.com/windows-hardware/drivers/debugger/bug-check-code-reference2>,
<https://learn.microsoft.com/windows-hardware/drivers/debugger/kernel-live-dump-code-reference>
(per-code pages are linked from both), and
<https://learn.microsoft.com/troubleshoot/windows-client/performance/event-id-41-restart>.

## Reading the code from events

- **Kernel-Power 41 `BugcheckCode` is DECIMAL.** Convert it before you look it up:
  159 = 0x9F, 278 = 0x116. The `Dec` column below saves you the conversion.
  `BugcheckCode=0` means no stop code was recorded. Then read the other fields:
  `PowerButtonTimestamp` nonzero means the user held the power button (a hang came first),
  and all zeros means power loss, a hard reset or a failed dump write (look for volmgr 46).
- WER-SystemErrorReporting 1001 ("rebooted from a bugcheck") gives the code in hex, as
  `0x00000116`.
- A LiveKernelEvent `P1` (Application log, WER 1001) is hex without `0x`: `141` = 0x141.
- **Live dump codes** (marked **LD**) "can never be used for a real bug check". The OS
  captured a dump and kept running. The dumps land in
  `C:\Windows\LiveKernelReports\<Component>\` (for example `WATCHDOG`).
  0x124 can be either a live dump or a real bug check.

## Table

| Code | Dec | Name | Meaning (Microsoft Learn) | Triage hint -> category |
|---|---|---|---|---|
| 0x0A | 10 | IRQL_NOT_LESS_OR_EQUAL | Windows or a kernel-mode driver accessed paged memory at an invalid address at raised IRQL (bad pointer or pageability) | Driver named in the dump, often after a new driver, BIOS or AV -> kernel_driver_fault |
| 0x1A | 26 | MEMORY_MANAGEMENT | Severe memory management error; P1 identifies the violation | Several P1 values (e.g. 0x31, 0x403) are "probably a hardware error"; run Windows Memory Diagnostic -> hardware_error, else kernel_driver_fault. Not memory_pressure |
| 0x3B | 59 | SYSTEM_SERVICE_EXCEPTION | Exception while running a routine that moves from non-privileged to privileged code; P1 = exception code (0xC0000005 = access violation) | Faulting module often a GPU driver (Microsoft's example: nvlddmkm.sys) -> kernel_driver_fault (name the driver) |
| 0x50 | 80 | PAGE_FAULT_IN_NONPAGED_AREA | Invalid system memory referenced (bad or freed address) | Faulty driver or service, AV, corrupted NTFS volume, or defective RAM / video RAM -> kernel_driver_fault; storage_io if NTFS or disk errors; hardware_error if RAM tests fail |
| 0x7E | 126 | SYSTEM_THREAD_EXCEPTION_NOT_HANDLED | A system thread raised an exception the handler didn't catch; P1 = exception, P2 = address (identifies the driver) | -> kernel_driver_fault |
| 0x9F | 159 | DRIVER_POWER_STATE_FAILURE | Driver is in an inconsistent or invalid power state; P1=0x3: a device object blocked an IRP too long | Sleep/resume/hibernate path; check `SleepInProgress` -> kernel_driver_fault. Not power_loss |
| 0xA0 | 160 | INTERNAL_POWER_ERROR | Fatal error in the power policy manager (many P1 values cover hibernation files and power-transition timeouts) | Firmware/driver power handling -> kernel_driver_fault. Not power_loss |
| 0xD1 | 209 | DRIVER_IRQL_NOT_LESS_OR_EQUAL | Kernel-mode driver touched pageable (or invalid) memory at too high an IRQL | Usually a bad driver pointer -> kernel_driver_fault (name the driver) |
| 0xEF | 239 | CRITICAL_PROCESS_DIED | A critical system process (csrss, wininit, smss, services, winlogon...) terminated | Check disk/NTFS errors first (storage_io); otherwise kernel_driver_fault |
| 0x101 | 257 | CLOCK_WATCHDOG_TIMEOUT | Expected clock interrupt on a secondary processor not received; processor hung or deadlocked | CPU instability, overclock, power or thermal -> hardware_error |
| 0x10E | 270 | VIDEO_MEMORY_MANAGEMENT_INTERNAL | Video memory manager hit an unrecoverable condition | "Usually caused by a video driver behaving improperly" -> kernel_driver_fault (GPU driver); gpu_driver_timeout only if TDR events come first |
| 0x116 | 278 | VIDEO_TDR_FAILURE | Resetting the display driver after a timeout failed; also raised when more than 5 TDRs happen in 1 minute (default) | Unrecovered TDR -> gpu_driver_timeout; check thermals, power, overclock (gpu_thermal / hardware_error) |
| 0x117 | 279 | VIDEO_TDR_TIMEOUT_DETECTED (**LD**) | Display driver failed to respond in time | Recovered TDR with a dump -> gpu_driver_timeout |
| 0x119 | 281 | VIDEO_SCHEDULER_INTERNAL_ERROR | Video scheduler detected a fatal violation; P1 0x1-0x13 = driver-reported, P1 0x400/0x1000/0xA000/0x10000 = internal state error "typically caused by memory corruption or bad hardware" | -> gpu_driver_timeout (unrecovered GPU failure); hardware_error if P1 is an internal-state value and WHEA/RAM evidence agrees |
| 0x124 | 292 | WHEA_UNCORRECTABLE_ERROR | Fatal hardware error reported via WHEA; P1 = error source type (0x0 machine check, 0x4 PCIe...) | Heat, defective hardware or RAM, failing CPU, overclock -> hardware_error; correlate with WHEA-Logger events |
| 0x133 | 307 | DPC_WATCHDOG_VIOLATION | One DPC/ISR ran too long (P1=0) or the system spent too long at DISPATCH_LEVEL or above (P1=1) | Faulty driver code -> kernel_driver_fault; storage_io if a storage driver plus disk errors |
| 0x139 | 313 | KERNEL_SECURITY_CHECK_FAILURE | Kernel detected corruption of a critical data structure; P1 = corruption type (3 = LIST_ENTRY) | -> kernel_driver_fault |
| 0x141 | 321 | VIDEO_ENGINE_TIMEOUT_DETECTED (**LD**) | One display engine failed to respond in time | Engine-level TDR. Doesn't count toward the 5-per-minute limit unless promoted -> gpu_driver_timeout |
| 0x142 | 322 | VIDEO_TDR_APPLICATION_BLOCKED (**LD**) | Logged when an engine reset blocks GPU access for the process causing timeouts | One app keeps hanging the GPU -> gpu_driver_timeout; name the app |
| 0x1A1 | 417 | WIN32K_CALLOUT_WATCHDOG_LIVEDUMP (**LD**) | A callout to Win32k did not return promptly | Desktop/UI hang -> application_hang if an app hang coincides; weak on its own |
| 0x1B0 | 432 | VIDEO_MINIPORT_FAILED_LIVEDUMP (**LD**) | dxgkrnl detected a video miniport driver failure; P1 0x1 = add device failed, 0x2 = start device failed | GPU driver failed to load or start, often after an update -> kernel_driver_fault (GPU driver) |

## Notes

- Name the driver only from evidence: the dump analysis, the WER bucket text (for example
  `IMAGE_nvlddmkm.sys`), or a vendor event. Never guess one from the code alone.
- Dumps: `C:\Windows\Minidump\`, `C:\Windows\MEMORY.DMP`, and
  `C:\Windows\LiveKernelReports\`. A human analyzes them with WinDbg `!analyze -v`.
