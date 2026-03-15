# ArgoCD CAPI Sync Controller

A lightweight, multi-context Python controller that automatically synchronizes Kubernetes Cluster API (CAPI) workload clusters to ArgoCD. It reads CAPI clusters, extracts their generated `kubeconfigs`, and safely pushes them as ArgoCD cluster secrets directly into a Kubernetes cluster or into a Git repository (GitOps mode).

## 💡 Motivation

The primary goal of this project is to provide a seamless **"auto-attach" mechanism** for CAPI (Cluster API) clusters to ArgoCD. 

By automatically synchronizing the cluster's access configuration (kubeconfig) along with its metadata (labels), it unlocks a fully automated, end-to-end GitOps workflow. This allows platform teams to dynamically leverage **ArgoCD Applications and ApplicationSets** to deploy workloads the exact moment a new tenant cluster is provisioned.

**Why not an Operator?** While a traditional Kubernetes Operator could perform similar tasks, this tool is specifically designed to interface with multiple *remote* clusters and contexts (Supervisor clusters, ArgoCD Hubs, Git repositories). A lightweight, multi-context CronJob/script approach is much more efficient, stateless, and flexible for cross-cluster routing than a standard Operator pattern, which is typically optimized for watching local cluster resources.

---

## 📖 Core Concepts

Understanding how the controller routes secrets is key to configuring it securely.

* **Supervisor Contexts (`SUPERVISOR_CONTEXTS`)**: The environments (Kubernetes contexts) where your CAPI `Cluster` resources reside. The script monitors these contexts for clusters with the sync label.
* **ArgoCD Contexts (`ARGOCD_CONTEXTS`)**: The *allowed* destinations where ArgoCD cluster secrets can be written. This acts as a strict security boundary. Even if a CAPI cluster requests a specific destination, it will be rejected if it's not listed here.
* **Destinations (`argocd-sync/destinations`)**: An annotation placed on the CAPI `Cluster` resource specifying exactly where its resulting ArgoCD secret should be pushed (e.g., a specific namespace, a remote cluster, or a Git repo path). Supports **multiple comma-separated destinations**.
* **Origin (`argocd-sync/origin`)**: An annotation automatically injected by the controller onto the generated ArgoCD secret. It traces the secret back to its source CAPI cluster (format: `<context>://<namespace>/<cluster-name>`). This is used for updates and Garbage Collection.
* **In-Cluster Mapping (`INCLUSTER_MAPPING`)**: When running the controller inside a Kubernetes cluster, the default context is natively `in-cluster`. While functional, this makes tracing origins confusing in a multi-cluster setup. It is highly recommended to map this to the actual name of your management cluster (e.g., `mgmt-cluster`). This ensures that origins and destinations remain readable and consistent.

---

## ✨ Features

* **Multi-Context & Multi-Namespace Support**: Sync clusters across different supervisor contexts and target specific ArgoCD namespaces.
* **GitOps Native**: Push ArgoCD cluster secrets directly to a Git repository instead of a live Kubernetes API.
* **Idempotent Updates**: Uses SHA256 hashing to ensure Kubernetes API patches or Git commits only happen when the underlying `kubeconfig` or labels actually change.
* **Built-in Garbage Collection**: Automatically detects and deletes orphan secrets (in K8s or Git) when a CAPI cluster is removed or the sync label is deleted.
* **Security-First**: Enforces strict boundaries. Destinations requested by CAPI annotations are validated against administrator-defined allowed contexts.
* **In-Cluster Friendly**: Includes identity mapping to seamlessly run as a Pod inside a Kubernetes cluster while maintaining readable origins.

---

## ⚙️ Configuration (Environment Variables & Values)

Configure the controller behavior using the following options. If you are deploying via **Carvel**, use the keys in the `Carvel Value` column inside your `values.yml`. If deploying via standard Kubernetes, set the `Environment Variable` on the Pod.

| Environment Variable | Carvel Value (`values.yml`) | Default Value | Description |
| :--- | :--- | :--- | :--- |
| `SUPERVISOR_CONTEXTS` | `supervisorContexts` | *(empty)* | **[Required]** Comma-separated list of allowed source contexts (e.g., `mgmt-cluster://capi-system, other-cluster://`). |
| `ARGOCD_CONTEXTS` | `argocdContexts` | *(empty)* | **[Required]** Comma-separated list of allowed K8s destinations (e.g., `mgmt-cluster://argocd`). |
| `SYNC_LABEL` | `syncLabel` | `argocd-sync/enabled` | The label used to discover eligible CAPI clusters and managed ArgoCD secrets. |
| `SYNC_LABEL_PREFIX` | `syncLabelPrefix` | `argocd-sync-label/` | Prefix of labels to dynamically copy from CAPI to the ArgoCD secret. |
| `ARGOCD_DESTINATION_ANNOTATION` | `argocdDestinationAnnotation` | `argocd-sync/destinations` | The annotation on the CAPI cluster defining where to push the secret. |
| `ARGOCD_DEFAULT_DESTINATION` | `argocdDefaultDestination` | `in-ns://` | Fallback destination if the destination annotation is missing. |
| `INCLUSTER_MAPPING` | `inclusterMapping` | *(empty)* | Maps the `in-cluster` execution to a human-readable context name (e.g., `mgmt-cluster`). |
| `INSECURE` | `insecure` | `false` | If `true`, sets `insecure: true` in the generated ArgoCD TLS config. |
| `LOG_LEVEL` | `logLevel` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

### External Cluster Configuration
| Carvel Value Key | Description |
| :--- | :--- |
| `additionalKubeconfig` | Optional YAML string containing a kubeconfig. When provided via installation values, it is mounted into the Pod to allow the controller to authenticate with external/remote clusters natively. |

### GitOps Configuration (Optional)
Required only if you are using `git#` destinations:

| Environment Variable | Carvel Value (`values.yml`) | Description |
| :--- | :--- | :--- |
| `GIT_CACHE_DIR` | `gitCacheDir` | Local directory to cache Git clones (default: `/tmp/argocd-sync-git`). |
| `GIT_USERNAME` | `gitUsername` | Username to authenticate HTTPS Git operations. |
| `GIT_TOKEN` | `gitToken` | Token/Password to authenticate HTTPS Git operations. |
| `GIT_BRANCH` | `gitBranch` | Target Git branch for pushing ArgoCD cluster secrets (default: `main`). |

---

## 📦 Installation

The controller can be deployed using various methods depending on your tooling preference.

### 1. Plain Kubernetes / Kustomize
If you prefer raw YAML or Kustomize, use the `deploy/base` directory.
```bash
# Apply directly
kubectl apply -k deploy/base/

# Or use it as a base in your own kustomization.yaml to patch environments/namespaces
```

### 2. Carvel (`ytt` & `kapp`)
To deploy using Carvel tools, populate your `values.yml` in the `deploy/carvel/config/` directory.
```bash
ytt -f deploy/carvel/config/deploy.yaml -f deploy/carvel/config/values.yml | kapp deploy -a capi-argocd-sync -f- -y
```

### 3. Supervisor Service (Carvel Package)
If you are running a Supervisor Server (e Tanzu Supervisor Services), you can register the generated package artifacts as a new supervisor service.

1. Go to Supervisor Management - Service 
2. Register a new service and upload the generated Artifact (package-capi-argocd-sync)
3. Go to your Supervisor server, Supervisor Service
4. Enable the service in the Available Service page.
5. Speficy your values file if required

---

## 🚀 Usage & Examples

### Labeling a CAPI Cluster
To enable synchronization for a workload cluster, simply apply the sync label:

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: my-workload-cluster
  namespace: capi-tenant-a
  labels:
    argocd-sync/enabled: "true"
    argocd-sync-label/env: "production" # This label will be copied to the ArgoCD secret
```

### Defining Destinations (Annotations)
You can define **one or multiple destinations (comma-separated)** using the destination annotation. 

**Push to the same namespace (Default):**
```yaml
  annotations:
    argocd-sync/destinations: "in-ns://"
```

**The `in-cluster://` Magic Context:**
If the controller is running inside a Kubernetes cluster, `in-cluster` automatically resolves to that local cluster. You can use it to push secrets to *other namespaces* on the same cluster without needing an external kubeconfig!
```yaml
  annotations:
    argocd-sync/destinations: "in-cluster://argocd"
```

**Push to multiple Kubernetes clusters:**
```yaml
  annotations:
    argocd-sync/destinations: "in-cluster://argocd, remote-hub://argocd-namespace"
```

**Push to a Git Repository (GitOps):**
*Format:* `git#<repo-url>/<file-path>`
```yaml
  annotations:
    argocd-sync/destinations: "git#https://github.com/my-org/my-repo.git/clusters/my-workload-cluster.yaml"
```

*Note: You can mix K8s and Git destinations entirely:*
`argocd-sync/destinations: "in-cluster://argocd, git#https://github.com/my-org/repo.git/path/file.yaml"`

### Helpful Commands
To quickly view the state of your synchronizations across your cluster, use these custom-column commands:

**View generated ArgoCD Secrets:**
```bash
kubectl get secret -l argocd-sync/enabled=true -A -o custom-columns="NAMESPACE:.metadata.namespace,NAME:.metadata.name,ORIGIN:.metadata.annotations.argocd-sync/origin"
```

**View configured CAPI Clusters:**
```bash
kubectl get clusters -A -o custom-columns="NAMESPACE:.metadata.namespace,NAME:.metadata.name,ENABLED:.metadata.labels.argocd-sync/enabled,DEST:.metadata.annotations.argocd-sync\.destinations"
```

---

## 🔐 Connecting to Remote Clusters

If you need the controller to monitor CAPI clusters or write ArgoCD secrets to a cluster *other* than the one it is running on, you must provide an `additionalKubeconfig`. 

Here is how to generate a scoped ServiceAccount and Kubeconfig on the remote cluster:

### 1. Create the ServiceAccount and RBAC on the Remote Cluster
```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: argocd-sync-remote-sa
  namespace: default
---
# Example: Role to allow writing ArgoCD secrets in a specific namespace
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argocd-sync-secret-manager
  namespace: argocd
rules:
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get", "list", "watch", "create", "patch", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: argocd-sync-secret-manager-binding
  namespace: argocd
subjects:
- kind: ServiceAccount
  name: argocd-sync-remote-sa
  namespace: default
roleRef:
  kind: Role
  name: argocd-sync-secret-manager
  apiGroup: rbac.authorization.k8s.io
```

### 2. Generate the Token and Kubeconfig
Extract the token for the ServiceAccount and build your kubeconfig block:
```bash
# Create a long-lived token (Kubernetes 1.24+)
kubectl create token argocd-sync-remote-sa --duration=8760h -n default
```

Place the resulting kubeconfig into your installation values (`values.yml` for Carvel):
```yaml
additionalKubeconfig: |
  apiVersion: v1
  kind: Config
  clusters:
  - name: remote-hub
    cluster:
      server: [https://api.remote-hub.com](https://api.remote-hub.com)
      certificate-authority-data: <base64-ca>
  users:
  - name: sync-sa
    user:
      token: "<token-from-command-above>"
  contexts:
  - name: remote-hub
    context:
      cluster: remote-hub
      user: sync-sa
```
Make sure `remote-hub://` is then added to your `SUPERVISOR_CONTEXTS` or `ARGOCD_CONTEXTS` variables as needed!