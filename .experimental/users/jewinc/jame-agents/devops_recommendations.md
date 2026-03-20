# DevOps Agent — Recommendations

Based on `agents/devops.py` and the AWS AI-DLC best practices from `aws_practices/`.

---

## Bonus — Post-MVP Improvements

These items were not implemented. They are listed here for future reference.

### B1 — Structured Pydantic output for DevOps artifacts

The DevOps agent still uses raw marker parsing (`===CICD_START===` / `===CICD_END===`). A malformed LLM response silently yields empty strings. Migrate to `llm.with_structured_output()` like the Developer and QA agents:

```python
class DevOpsArtifacts(BaseModel):
    cicd_yaml: str = Field(description="GitHub Actions workflow YAML")
    dockerfile: str = Field(description="Production Dockerfile")
    docker_compose_yaml: str = Field(description="docker-compose.yml for local dev")
    dockerignore: str = Field(description=".dockerignore content")
```

### B2 — OIDC for cloud authentication

Add an OIDC-based AWS role assumption step to the CI workflow so no static credentials are stored in GitHub Secrets:

```yaml
- uses: aws-actions/configure-aws-credentials@<SHA>
  with:
    role-to-assume: arn:aws:iam::<account>:role/<role>
    aws-region: us-east-1
```

Reference: `aws_practices/aidlc-rules/.../infrastructure-design.md`

### B3 — Artifact upload to GitHub Releases

After the build step, upload the distributable artifact so it is traceable per commit:

```yaml
- uses: actions/upload-artifact@<SHA>
  with:
    name: dist
    path: dist/
```

### B4 — SBOM generation (SECURITY-10)

Add a `cyclonedx-bom` step to produce a software bill of materials alongside `pip-audit`:

```yaml
- run: pip install cyclonedx-bom && cyclonedx-py -o sbom.json
- uses: actions/upload-artifact@<SHA>
  with:
    name: sbom
    path: sbom.json
```

### B5 — Credential management guidance (SECURITY-12)

Inject a `CONTRIBUTING.md` or `.env.example` into the generated project explaining that all credentials must be stored in GitHub Secrets and never hardcoded.

### B6 — Artifact integrity verification (SECURITY-13)

Add a SHA-256 checksum step after build:

```yaml
- run: sha256sum dist/* > dist/checksums.txt
```

### B7 — `Makefile` DX artifact

Generate a `Makefile` alongside the other artifacts for developer convenience:

```makefile
.PHONY: test lint build run

test:
	pytest --tb=short

lint:
	ruff check .

build:
	docker build -t app .

run:
	docker-compose up --build
```

---

## AWS Reference Files

| Topic | File |
|-------|------|
| CI/CD pipeline architecture | `aws_practices/docs/ADMINISTRATIVE_GUIDE.md` |
| Build & test strategy | `aws_practices/aidlc-rules/aws-aidlc-rule-details/construction/build-and-test.md` |
| Security baseline (15 rules) | `aws_practices/aidlc-rules/aws-aidlc-rule-details/extensions/security/baseline/security-baseline.md` |
| Infrastructure design | `aws_practices/aidlc-rules/aws-aidlc-rule-details/construction/infrastructure-design.md` |
| Local build/test guide | `aws_practices/docs/DEVELOPERS_GUIDE.md` |
