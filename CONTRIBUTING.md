# Contributing to Blade EC Control

Thanks for helping us understand how Razer Blade embedded controllers work. Hardware test results, documentation fixes, and improvements to either app are all welcome.

## Share a finding

If you have a Razer Blade, try the tools on your own laptop and tell us what you find. Successful tests, failures, and results specific to one model all help. Open a pull request adding your report to [FINDINGS.md](FINDINGS.md).

We merge findings even when they still need confirmation. Label those entries **Unverified**, describe what you observed, and say what still needs checking.

Include what you know about your setup and results.

- Your exact Blade model number (for example, `RZ09-0328`), BIOS and EC firmware versions, and whether your EC firmware is stock or modified.
- The app version or commit you tested, the steps you took, and what you expected and observed.
- Relevant readings, log excerpts, or screenshots, if available.

You can also share an observation or ask a question through [GitHub Issues](https://github.com/GepingY/Blade-EC-Control/issues).

## Improve either app

Both apps are due for a GUI overhaul. GUI development is currently paused, so usability fixes and redesigns are especially welcome and likely to be merged. Screenshots or a short recording help reviewers see what changed.

- `controller/` contains the Blade Fan Control GUI and its Windows launchers. [Controller README](controller/README.md)
- `patcher/` contains the EC Auto-Patcher's Python core. The current GUI ships in the Windows release, and a new GUI built on the existing core is welcome. [Patcher README](patcher/README.md)

Changes to either app's behavior receive closer review before merging, especially changes to fan or power control, ROM validation, or patch output. Explain what behavior changes and which tests you ran.

## Open a pull request

1. Fork the repository on GitHub and create a branch from `main`.
2. Make your changes and commit them. For app changes, run the applicable checks listed in the app's README.
3. Push your branch to your fork and open a pull request targeting `main`. Summarize what you found or changed and include any test results.
