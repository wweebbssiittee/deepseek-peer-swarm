# Upload to GitHub

Suggested repository name: **deepseek-peer-swarm**

Suggested description:

> Local multi-agent AI harness: ten equal peers, shared task board, peer review, durable recovery, spending controls, and a live dashboard.

Suggested topics: `multi-agent`, `ai-agents`, `deepseek`, `python`, `fastapi`,
`agent-harness`, `a2a`.

## Upload through your browser

1. Extract the source ZIP. Open the extracted `deepseek-peer-swarm` folder.
2. On GitHub, create a new **public** repository named `deepseek-peer-swarm`.
   Leave the automatic README, license and ignore-file options empty: this
   package already includes them.
3. On the empty repository page, choose **uploading an existing file**. In an
   existing repository, use **Add file > Upload files**.
4. Drag the **contents inside** the extracted folder into the upload area. The
   README and license should appear at the repository root, not inside another
   nested project folder. Make sure `.github`, `.gitignore` and `.gitattributes`
   are included; show hidden items in your file manager if necessary.
5. Enter a commit message such as `Initial release of DeepSeek Peer Swarm` and
   commit the upload. Add the description and topics in the repository's About
   panel. Check the Actions tab for the automated checks.

Upload the extracted source files, not just the ZIP, so GitHub can display the
README, browse the code and run the workflow. You can also attach the ZIP to a
GitHub Release later.

The included MIT license permits free use, modification and redistribution,
including commercial use, with the license notice retained. The software is free;
live DeepSeek API requests use each person's own keys and provider balance.

## Rebuild the package

From the project root, with Python 3.11 or newer (Git is optional):

```powershell
python scripts/check_publication.py
python scripts/package_release.py
```

The packaging script scans publication candidates, writes a versioned ZIP under
`dist/`, verifies every archived file against the source, and creates a SHA-256
checksum alongside it. It does not create a remote repository or upload files.
Screenshots still need visual review whenever they change.

References: [GitHub's upload instructions](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)
and the [MIT license](https://opensource.org/license/mit).
