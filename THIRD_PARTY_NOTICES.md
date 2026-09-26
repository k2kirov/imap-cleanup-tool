# Third-party rule data

The auto-sort feature ships header-only sorting data from these sources.
The app never downloads rules at runtime.

| File | Source | Commit | License |
|---|---|---|---|
| `src/imap_cleanup_tool/inpector_rules.json` | https://github.com/inpector/sieve-filters | fe5a0ce637504c6baf70514f13f369f09d234de0 | CC0-1.0 |
| `src/imap_cleanup_tool/poli0981_rules.json` | https://github.com/poli0981/proton-sieve-filters (`data/categories/*.yml`) | 20fe383b94db4894f9a2a8525a807f65d9ed232b | CC0-1.0 |
| `src/imap_cleanup_tool/scrothers_rules.json` | https://github.com/scrothers/sieve-filters | 76d7728181cb2b2df3c072e242a0bd7e9b938873 | MIT |

Regenerate the poli0981 file with `python scripts/update_rulepacks.py <commit>`.

## scrothers/sieve-filters (MIT)

MIT License

Copyright (c) 2022 Steven Crothers

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
