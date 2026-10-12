# Upload to GitHub

Install Git if `git --version` is unavailable. Open PowerShell in this project
folder and create an empty repository on GitHub with your preferred visibility.
Leave GitHub's README, license and gitignore initialization options unchecked.

## Files

| Included in Git | Kept locally |
| --- | --- |
| `ministry_bot/`, `tests/`, `assets/` | `.venv/`, `__pycache__/` |
| `README.md`, `GITHUB.md`, `requirements.txt` | `.env` |
| `.gitignore`, `.env.example` | `data/`, `logs/` |

The clean ZIP is a local replacement package and includes your configuration
and database. Push the extracted project files using Git.

## First push

Replace the example URL with your repository's HTTPS URL:

```powershell
git init -b main
git add .
git status --short
git commit -m "Initial bot setup"
git remote add origin https://github.com/YOUR_USERNAME/ministry-of-digital-governance.git
git push -u origin main
```

If Git requests a commit identity, set it for this repository and retry the commit:

```powershell
git config user.name "Your Name"
git config user.email "YOUR_GITHUB_EMAIL"
```

Complete GitHub sign-in when prompted. Git Credential Manager supports browser
sign-in over HTTPS. Use your GitHub account for this step.

## Later updates

```powershell
git add .
git status --short
git commit -m "Describe the changes"
git push
```

Official guides: [first upload](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github),
[GitHub sign-in](https://docs.github.com/en/get-started/git-basics/caching-your-github-credentials-in-git).
