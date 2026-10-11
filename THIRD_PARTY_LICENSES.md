# Third-party licenses / Drittanbieter-Lizenzen

githubbot-core contains **no third-party code** and has **no runtime dependencies** (Python
standard library only). The following tools are used but **not bundled or redistributed**:

| Component | Used for | License | Bundled |
|---|---|---|---|
| [GitHub CLI (`gh`)](https://github.com/cli/cli) | default transport and token lookup, invoked as an external program | MIT | no |
| [OpenSSH `ssh-keygen`](https://www.openssh.com/) | signing and verifying review-gate receipts, invoked as an external program | BSD-style | no |
| [actions/checkout](https://github.com/actions/checkout) | referenced (pinned by commit SHA) in the review-gate workflow template | MIT | no |
| [setuptools](https://github.com/pypa/setuptools) | build backend (build time only) | MIT | no |
| [pytest](https://github.com/pytest-dev/pytest) | tests (development only) | MIT | no |
| [Ruff](https://github.com/astral-sh/ruff) | linting (development only) | MIT | no |

githubbot-core enthält **keinen Fremdcode** und hat **keine Laufzeitabhängigkeiten**. Die oben
genannten Werkzeuge werden genutzt, aber weder mitgeliefert noch weiterverteilt.
