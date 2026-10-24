# TDR checklist

Load this when `gpu_driver_timeout` is a top-2 candidate, when you write GPU next steps, or
when the user asks about TDR, TdrDelay or "display driver stopped responding".
Sources (Microsoft Learn, checked 2026-10-23):
<https://learn.microsoft.com/windows-hardware/drivers/display/timeout-detection-and-recovery>,
<https://learn.microsoft.com/windows-hardware/drivers/display/tdr-registry-keys>, and the
0x116, 0x117 and 0x141 pages. Lines marked *(community)* are vendor events that Microsoft
does not document; use them as corroboration, never as the only proof.

## How TDR works

1. **Detect.** The GPU scheduler in `dxgkrnl.sys` notices a GPU task running too long and
   tries to preempt it. The preempt wait is the TDR timeout, 2 s by default (`TdrDelay`). If
   the GPU can't finish or preempt in time, the OS treats the GPU as frozen.
2. **Prepare.** The OS calls the driver's `DxgkDdiResetFromTimeout`. The driver stops
   touching hardware and memory, then reinitializes and resets the GPU. If driver threads
   don't leave within `TdrDdiDelay` (5 s by default), the OS bug checks with **0x116**.
3. **Recover.** The video memory manager purges video memory and the driver resets the GPU.
   The user sees a flicker or a brief black screen. Windows reports "Display driver stopped
   responding and has recovered" and logs it to Event Viewer (Display 4101) along with a
   debug report. Some older DirectX apps stay black until restarted.
4. **Limit.** By default the OS bug checks on the sixth GPU hang once five or more hangs
   (0x117) and recoveries happen within one minute (`TdrLimitCount` 5, `TdrLimitTime` 60 s).
   Engine timeouts (0x141) don't count unless they're promoted to an adapter reset. 0x142
   means the offending process was blocked from the GPU.

## Recovered vs unrecovered

| Outcome | Evidence | What it means |
|---|---|---|
| Recovered TDR | System **Display 4101** "Display driver %1 stopped responding and has successfully recovered." (`%1` = `nvlddmkm`, `amdkmdag`/`amdkmdap`, Intel `igfx*`) | GPU reset worked and the desktop came back |
| Vendor corroboration *(community)* | **nvlddmkm 153** = TDR/reset ("Reset TDR occurred on GPUID..."); **nvlddmkm 13** = Graphics Exception (likely); **nvlddmkm 14** = nvlddmkm error, meaning not publicly documented; **amdkmdag** errors at the same time | Same incident, seen from the driver's side. Windows event IDs are **not** NVIDIA Xid numbers |
| Recovered, live dump captured | Application log **Windows Error Reporting 1001**, `Event Name: LiveKernelEvent`, **P1 141 or 117**, dump in `C:\Windows\LiveKernelReports\WATCHDOG\` | A live dump, never a crash. The OS kept running. This is strong timing and driver evidence; the bucket text often names the image, e.g. `IMAGE_nvlddmkm.sys` |
| Unrecovered (crash) | **Kernel-Power 41** with `BugcheckCode` 278 (0x116), 281 (0x119) or 270 (0x10E), plus **WER-SystemErrorReporting 1001** "rebooted from a bugcheck", plus a minidump | Recovery failed, or more than 5 TDRs in 60 s. A real BSOD and reboot |
| Unrecovered (hard hang) | TDR or vendor events, then **Kernel-Power 41 `BugcheckCode=0`** with a nonzero `PowerButtonTimestamp` | The black screen never returned and the user held the power button. Still gpu_driver_timeout when TDR events come first |

Decision cues:
- A single recovered TDR during heavy GPU load is medium severity. Repeats within minutes,
  or a 0x116, are high or critical.
- TDRs that start right after a driver install (UserPnp 20001 or WindowsUpdateClient 19
  in the digest) point to the driver version. Name it in the bug report.
- TDRs together with GPU temperatures at or near the vendor limit point to `gpu_thermal`.
  You need `sensor_snapshot` data from the incident window; a TDR alone is not thermal.
- TDRs together with WHEA 18/46, a 0x124, or a reboot with all-zero Kernel-Power 41 fields
  point to `hardware_error` or `power_loss` (PSU) as the stronger or co-ranked cause.

## Next steps (vendor-neutral, all done by the user)

1. **Clean driver reinstall** with the GPU vendor's own installer, using its clean-install
   or factory-reset option. If the trouble began right after an update, roll back to the
   previous version instead (Device Manager > Display adapters > Roll Back Driver).
2. **Thermals.** Watch GPU temperature and fan speed under the failing workload, clean
   dust, and check case airflow. Compare with the vendor's rated maximum.
3. **Power / PSU.** Check PSU wattage headroom for the GPU, use separate PCIe power cables
   (no daisy-chained splitters), reseat the connectors, and rule out a failing PSU or UPS.
4. **Overclock rollback.** Return GPU, CPU and RAM to stock (including factory-OC profiles
   and XMP/EXPO) and retest. Microsoft lists overclocking, cooling and power among the
   0x116 causes.
5. **Reliability Monitor.** Open "View reliability history" (`perfmon /rel`) and look for
   repeating "Hardware error" or "Windows stopped working" entries and when they started.
6. **Collect dumps** for the vendor report: `C:\Windows\LiveKernelReports\WATCHDOG\*.dmp`,
   `C:\Windows\Minidump\*.dmp` and `C:\Windows\MEMORY.DMP`. Reading them may need admin
   rights. Attach them to the bug report with the driver version and OS build.
7. **Isolate the workload.** Reproduce it with one app, update that app, and turn off
   overlays and capture tools. Try another cable or port and a single monitor.

## TdrDelay and other TDR registry keys (documentation only)

The keys live under `HKLM\System\CurrentControlSet\Control\GraphicsDrivers` (REG_DWORD):
`TdrLevel` (0 off, 1 bug check, 2 recover to VGA (not implemented), 3 recover, the
default), `TdrDelay` (2 s), `TdrDdiDelay` (5 s), `TdrDebugMode` (2), `TdrLimitTime` (60 s),
`TdrLimitCount` (5).

Microsoft documents these for **testing and debugging during driver development** and says
**"End users shouldn't manipulate these registry keys."** The agent never changes or
scripts them and never presents them as a fix. You may mention `TdrDelay` only as a
documented diagnostic option that a human decides on, with this warning: raising it hides
hangs instead of fixing them, and the screen stays frozen longer before Windows recovers.
Back up the registry first and undo the change after testing.
