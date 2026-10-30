param(
    [Parameter(Mandatory = $true)][string]$AwsProfile,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    & $Python scripts/prepare-gitops.py --check
    if ($LASTEXITCODE -ne 0) { throw "Fill and validate gitops/config.yaml before bootstrap." }
    $gitopsConfig = (& $Python -c "import json,yaml; print(json.dumps(yaml.safe_load(open('gitops/config.yaml'))))") | ConvertFrom-Json
    $versions = (& $Python -c "import json,yaml; print(json.dumps(yaml.safe_load(open('gitops/versions.yaml'))))") | ConvertFrom-Json
    $actualAccount = (& aws sts get-caller-identity --profile $AwsProfile --query Account --output text).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualAccount -ne $gitopsConfig.accountId) { throw "AWS profile does not match the configured account." }
    & aws eks update-kubeconfig --name $gitopsConfig.clusterName --region $gitopsConfig.region --profile $AwsProfile --alias $gitopsConfig.clusterName
    if ($LASTEXITCODE -ne 0) { throw "Could not configure access to the EKS cluster." }
    & kubectl --context $gitopsConfig.clusterName auth can-i '*' '*' --all-namespaces
    if ($LASTEXITCODE -ne 0) { throw "Bootstrap needs the administration EKS Access Entry." }
    # Install ESO first so even Argo CD's initial credentials originate in AWS.
    # retail-root later reconciles this exact release name/version/namespace.
    & helm upgrade --install external-secrets external-secrets --repo $versions.'external-secrets'.repository --version $versions.'external-secrets'.version --namespace external-secrets --create-namespace --kube-context $gitopsConfig.clusterName -f gitops/platform/external-secrets.yaml --wait --timeout 15m
    if ($LASTEXITCODE -ne 0) { throw "External Secrets Operator did not become ready." }
    & helm template retail-argocd-secrets gitops/argocd-secrets --namespace argocd -f gitops/config.yaml | & kubectl --context $gitopsConfig.clusterName apply -f -
    if ($LASTEXITCODE -ne 0) { throw "Could not declare Argo CD credential references." }
    & kubectl --context $gitopsConfig.clusterName -n argocd wait externalsecret/argocd-admin externalsecret/argocd-redis --for=condition=Ready --timeout=5m
    if ($LASTEXITCODE -ne 0) { throw "Argo CD credentials have not synchronized from Secrets Manager." }
    & helm upgrade --install argocd argo-cd --repo $versions.'argo-cd'.repository --version $versions.'argo-cd'.version --namespace argocd --create-namespace --kube-context $gitopsConfig.clusterName -f gitops/platform/argo-cd.yaml --wait --timeout 15m
    if ($LASTEXITCODE -ne 0) { throw "Argo CD installation did not become ready." }
    $application = @{
        apiVersion = "argoproj.io/v1alpha1"
        kind = "Application"
        metadata = @{ name = "retail-root"; namespace = "argocd" }
        spec = @{
            project = "default"
            source = @{ repoURL = $gitopsConfig.gitopsRepo; targetRevision = $gitopsConfig.gitopsRevision; path = "bootstrap"; helm = @{ valueFiles = @("../config.yaml", "../versions.yaml") } }
            destination = @{ server = "https://kubernetes.default.svc"; namespace = "argocd" }
            syncPolicy = @{ automated = @{ selfHeal = $true; prune = $false }; syncOptions = @("ServerSideApply=true") }
        }
    }
    $application | ConvertTo-Json -Depth 20 | & kubectl --context $gitopsConfig.clusterName apply -f -
    if ($LASTEXITCODE -ne 0) { throw "Could not create the root GitOps Application." }
    Write-Host "Argo CD bootstrap completed. Check retail-root before enabling image PR automation."
} finally {
    Pop-Location
}
