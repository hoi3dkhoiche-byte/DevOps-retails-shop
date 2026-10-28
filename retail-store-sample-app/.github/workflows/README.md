# Archived upstream workflows

The YAML files in this nested directory are retained as upstream reference only. GitHub Actions runs the root `.github/workflows/ci.yml`. Do not copy these workflows to the root: their ECR/release callers bypass the new image gates. Release-please and OSS publication are intentionally inactive.
