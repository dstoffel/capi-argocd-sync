import logging
import os
import re
import base64
import yaml
import json
import urllib3
import hashlib
import glob
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import git

urllib3.disable_warnings()

ARGOCD_DEFAULT_DESTINATION = os.getenv("ARGOCD_DEFAULT_DESTINATION", "in-ns://")
SYNC_LABEL = os.getenv("SYNC_LABEL", "argocd-sync/enabled")
SYNC_LABEL_PREFIX = os.getenv("SYNC_LABEL_PREFIX", "argocd-sync-label/")
ARGOCD_DESTINATION_ANNOTATION = os.getenv("ARGOCD_DESTINATION_ANNOTATION", "argocd-sync/destinations")
ORIGIN_ANNOTATION = "argocd-sync/origin"
HASH_ANNOTATION = "argocd-sync/sha256"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
INSECURE = os.getenv("INSECURE", "").lower() == "true"
SUPERVISOR_CONTEXTS = os.getenv("SUPERVISOR_CONTEXTS", "")
ARGOCD_CONTEXTS = os.getenv("ARGOCD_CONTEXTS", "")

INCLUSTER_MAPPING = os.getenv("INCLUSTER_MAPPING", "").strip()
INCLUSTER_NAME = INCLUSTER_MAPPING if INCLUSTER_MAPPING else "in-cluster"

GIT_CACHE_DIR = os.getenv("GIT_CACHE_DIR", "/tmp/argocd-sync-git")
GIT_USERNAME = os.getenv("GIT_USERNAME", "")
GIT_TOKEN = os.getenv("GIT_TOKEN", "")
GIT_BRANCH = os.getenv("GIT_BRANCH", "main")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argocd-sync")

ORIGIN_REGEX = re.compile(r"^(?P<sup_ctx>[^:]+)://(?P<ns>[^/]+)/(?P<cluster>.+)$")
DEST_REGEX = re.compile(r"^(?P<ctx>[^:]+)://(?P<namespace>.+)$")
GIT_DEST_REGEX = re.compile(r"^git#(?P<repo>https?://.+?\.git)/(?P<path>.+\.ya?ml)$")
GIT_TARGET_REGEX = re.compile(r"^git#(?P<repo>https?://.+?\.git)(?:/(?P<path>.*))?$")


class GitManager:
    def __init__(self):
        self.cache_dir = GIT_CACHE_DIR
        self.repos = {}
        os.makedirs(self.cache_dir, exist_ok=True)

    def _inject_auth(self, repo_url):
        if GIT_USERNAME and GIT_TOKEN and repo_url.startswith("https://"):
            return repo_url.replace("https://", f"https://{GIT_USERNAME}:{GIT_TOKEN}@")
        return repo_url

    def get_repo_local_path(self, repo_url):
        repo_hash = hashlib.md5(repo_url.encode()).hexdigest()
        return os.path.join(self.cache_dir, repo_hash)

    def prepare_repo(self, repo_url):
        if repo_url in self.repos:
            return self.get_repo_local_path(repo_url)

        local_path = self.get_repo_local_path(repo_url)
        auth_url = self._inject_auth(repo_url)

        try:
            if os.path.exists(os.path.join(local_path, ".git")):
                log.info(f"Git Pulling latest changes for {repo_url} on branch '{GIT_BRANCH}'...")
                repo = git.Repo(local_path)
                repo.git.checkout(GIT_BRANCH)
                # On passe l'URL avec les identifiants directement à la commande pull
                repo.git.pull(auth_url, GIT_BRANCH)
            else:
                log.info(f"Git Cloning {repo_url} (branch: '{GIT_BRANCH}')...")
                repo = git.Repo.clone_from(auth_url, local_path, branch=GIT_BRANCH)
                # Sécurité : On retire immédiatement les identifiants du cache local (.git/config)
                repo.remotes.origin.set_url(repo_url)
            
            self.repos[repo_url] = repo
            return local_path
        except Exception as e:
            log.error(f"Git Error on {repo_url}: {e}")
            return None

    def commit_and_push_all(self):
        for repo_url, repo in self.repos.items():
            try:
                if repo.is_dirty(untracked_files=True):
                    log.info(f"Git changes detected in {repo_url}, committing and pushing to branch '{GIT_BRANCH}'...")
                    repo.git.add(A=True)
                    repo.index.commit("Auto-sync ArgoCD clusters from CAPI")
                    
                    auth_url = self._inject_auth(repo_url)
                    # On pousse directement vers l'URL authentifiée sans modifier le repo local
                    repo.git.push(auth_url, GIT_BRANCH)
                    log.info(f"Successfully pushed changes to {repo_url}")
                else:
                    log.info(f"No Git changes for {repo_url}, skipping push.")
            except Exception as e:
                log.error(f"Failed to push {repo_url}: {e}")

class KubeManager:
    def __init__(self):
        self.clients = self._get_all_kube_clients()

    def _get_all_kube_clients(self):
        clients = {}
        try:
            contexts, _ = config.list_kube_config_contexts()
            for ctx in contexts:
                original_name = ctx['name']
                mapped_name = INCLUSTER_NAME if original_name == 'in-cluster' else original_name
                try:
                    conf = client.Configuration()
                    config.load_kube_config(context=original_name, client_configuration=conf)
                    api_client = client.ApiClient(conf)
                    clients[mapped_name] = api_client
                except Exception:
                    pass
        except Exception:
            pass

        try:
            config.load_incluster_config()
            conf = client.Configuration.get_default_copy()
            api_client = client.ApiClient(conf)
            clients[INCLUSTER_NAME] = api_client
        except Exception:
            pass

        return clients

    def core_v1(self, context):
        if context in self.clients:
            return client.CoreV1Api(self.clients[context])
        raise ValueError(f"Invalid context: {context}")

    def custom_objects(self, context):
        if context in self.clients:
            return client.CustomObjectsApi(self.clients[context])
        raise ValueError(f"Invalid context: {context}")
    
    def get_argocd_clusters(self, target, valid_supervisor_targets):
        all_clusters = {}
        parts = target.split('://')
        context = parts[0]
        specific_namespace = parts[1] if len(parts) > 1 and parts[1] else None

        try:
            v1 = self.core_v1(context)
            if specific_namespace:
                secrets = v1.list_namespaced_secret(namespace=specific_namespace, label_selector=f"{SYNC_LABEL}=true")
            else:
                secrets = v1.list_secret_for_all_namespaces(label_selector=f"{SYNC_LABEL}=true")
        except ApiException as e:
            log.error(f"Error while fetching ArgoCD clusters for {target}: {e}")
            return {}

        for secret in secrets.items:
            annotations = secret.metadata.annotations or {}
            origin = annotations.get(ORIGIN_ANNOTATION)
            secret_hash = annotations.get(HASH_ANNOTATION)
            
            if not origin: continue
            match = ORIGIN_REGEX.match(origin)
            if not match: continue

            sup_ctx, origin_ns = match.group('sup_ctx'), match.group('ns')
            
            is_allowed = any(vt['ctx'] == sup_ctx and (vt['ns'] is None or vt['ns'] == origin_ns) for vt in valid_supervisor_targets)
            if not is_allowed: continue
                
            secret_path = f"k8s#{context}://{secret.metadata.namespace}/{secret.metadata.name}"
            all_clusters[secret_path] = {
                'type': 'k8s',
                'context': context,
                'namespace': secret.metadata.namespace,
                'name': secret.metadata.name,
                'origin': origin,
                'hash': secret_hash,
            }
            
        return all_clusters

    def get_capi_clusters(self, target, valid_argocd_targets, git_manager):
        all_clusters = {}
        parts = target.split('://')
        context = parts[0]
        specific_namespace = parts[1] if len(parts) > 1 and parts[1] else None
        group, version, plural = "cluster.x-k8s.io", "v1beta2", "clusters"
        
        try:
            custom_api = self.custom_objects(context)
            if specific_namespace:
                response = custom_api.list_namespaced_custom_object(group=group, version=version, namespace=specific_namespace, plural=plural, label_selector=f"{SYNC_LABEL}=true")
            else:
                response = custom_api.list_cluster_custom_object(group=group, version=version, plural=plural, label_selector=f"{SYNC_LABEL}=true")
        except ApiException as e:
            return {}
        
        for cluster in response.get("items", []):
            cluster_name = cluster['metadata']['name']
            cluster_namespace = cluster['metadata']['namespace']
            cluster_path = f"{context}://{cluster_namespace}/{cluster_name}"
            
            try:
                kubeconfig_secret = self.core_v1(context).read_namespaced_secret(f"{cluster_name}-kubeconfig", cluster_namespace)
                server, ca_data, cert_data, key_data = extract_tls_from_kubeconfig(kubeconfig_secret.data['value'])
            except Exception:
                continue
            
            raw_labels = cluster['metadata'].get('labels', {})
            filtered_labels = {k: v for k, v in raw_labels.items() if k.startswith(SYNC_LABEL_PREFIX)}
                
            cc = {
                'clusterPath': cluster_path,
                'name': cluster_name,
                'namespace': cluster_namespace,
                'context': context,
                'labels': filtered_labels,
                'kubeconfig': {'server': server, 'ca_data': ca_data, 'cert_data': cert_data, 'key_data': key_data}
            }
            
            annotations = cluster['metadata'].get('annotations', {})
            destinations = annotations.get(ARGOCD_DESTINATION_ANNOTATION, ARGOCD_DEFAULT_DESTINATION)
            validated_destinations = []
            
            for destination in destinations.split(','):
                destination = destination.strip()
                
                if destination.startswith('git#'):
                    match = GIT_DEST_REGEX.match(destination)
                    if match:
                        repo_url = match.group('repo')
                        file_path = match.group('path')
                        
                        is_allowed = False
                        for vt in valid_argocd_targets:
                            if vt['type'] == 'git' and vt['repo'] == repo_url:
                                allowed_prefix = vt['path'] + '/' if vt['path'] else ""
                                if file_path.startswith(allowed_prefix) or file_path == vt['path']:
                                    is_allowed = True
                                    break
                                    
                        if not is_allowed:
                            log.warning(f"Git destination '{destination}' rejected: not allowed by ARGOCD_CONTEXTS.")
                            continue
                        
                        local_repo_path = git_manager.prepare_repo(repo_url)
                        if local_repo_path:
                            validated_destinations.append({
                                'type': 'git',
                                'destinationPath': destination,
                                'repo_url': repo_url,
                                'file_path': file_path,
                                'local_repo_path': local_repo_path
                            })
                    continue
                
                match = DEST_REGEX.match(destination)
                if destination == 'in-ns://':
                    _ctx, _ns = context, cluster_namespace
                elif match:
                    _ctx = context if match.group('ctx') == 'in-cluster' else match.group('ctx')
                    _ns = match.group('namespace')
                    if _ctx not in self.clients:
                        continue
                else:
                    continue
                
                real_destination = f"{_ctx}://{_ns}"
                is_allowed = any(vt['ctx'] == _ctx and (vt['ns'] is None or vt['ns'] == _ns) for vt in valid_argocd_targets if vt['type'] == 'k8s')
                
                if is_allowed:
                    validated_destinations.append({
                        'type': 'k8s',
                        'destinationPath': real_destination, 
                        'context': _ctx, 
                        'namespace': _ns
                    })
                
            if validated_destinations:
                cc['destinations'] = validated_destinations
                all_clusters[cluster_path] = cc
                dest_paths = ', '.join([x['destinationPath'] for x in validated_destinations])
                log.info(f"Resolved CAPI Cluster {cluster_path} to destinations: {dest_paths}")

        return all_clusters


def extract_tls_from_kubeconfig(kubeconfig_b64):
    kubeconfig_yaml = base64.b64decode(kubeconfig_b64).decode("utf-8")
    kubeconfig = yaml.safe_load(kubeconfig_yaml)
    cluster_info = kubeconfig["clusters"][0]["cluster"]
    user_info = kubeconfig["users"][0]["user"]
    return (cluster_info["server"], cluster_info["certificate-authority-data"], user_info["client-certificate-data"], user_info["client-key-data"])


def get_valid_targets(env_var_value, kube_manager, component_name):
    valid_targets = []
    for target in env_var_value.split(','):
        target = target.strip()
        if not target: continue
        
        if target.startswith('git#'):
            match = GIT_TARGET_REGEX.match(target)
            if match:
                path = match.group('path') or ""
                valid_targets.append({'type': 'git', 'raw': target, 'repo': match.group('repo'), 'path': path.strip('/')})
            else:
                log.warning(f"Invalid Git format for {component_name} target '{target}'.")
            continue

        if '://' not in target:
            continue
            
        parts = target.split('://')
        ctx = parts[0]
        ns = parts[1] if len(parts) > 1 and parts[1] else None
        if ctx not in kube_manager.clients: continue
        valid_targets.append({'type': 'k8s', 'raw': target, 'ctx': ctx, 'ns': ns})
        
    log.info(f"Validated {component_name} targets: {', '.join([t['raw'] for t in valid_targets])}")
    return valid_targets


def get_all_argocd_clusters(kube_manager, valid_argocd_targets, valid_supervisor_targets):
    all_clusters = {}
    for target in valid_argocd_targets:
        if target['type'] == 'k8s':
            clusters = kube_manager.get_argocd_clusters(target['raw'], valid_supervisor_targets)
            all_clusters.update(clusters)
            log.info(f"Clusters ArgoCD (K8s)[{SYNC_LABEL}=true] in {target['raw']}: {len(clusters)}")
    return all_clusters


def get_all_git_clusters(git_manager, valid_argocd_targets, valid_supervisor_targets):
    all_clusters = {}
    
    for vt in valid_argocd_targets:
        if vt['type'] != 'git': continue
        
        repo_url = vt['repo']
        allowed_path = vt['path']
        local_path = git_manager.get_repo_local_path(repo_url)
        
        if not os.path.exists(local_path): continue
            
        search_path = os.path.join(local_path, allowed_path) if allowed_path else local_path
        
        yaml_files = glob.glob(os.path.join(search_path, '**/*.yaml'), recursive=True) + \
                     glob.glob(os.path.join(search_path, '**/*.yml'), recursive=True)
        
        count = 0
        for file_path in yaml_files:
            try:
                with open(file_path, 'r') as f:
                    docs = yaml.safe_load_all(f)
                    for doc in docs:
                        if not doc or doc.get('kind') != 'Secret': continue
                        
                        metadata = doc.get('metadata', {})
                        labels = metadata.get('labels', {})
                        annotations = metadata.get('annotations', {})
                        
                        if labels.get('managed-by') != 'argocd-sync' or labels.get('argocd.argoproj.io/secret-type') != 'cluster':
                            continue
                            
                        origin = annotations.get(ORIGIN_ANNOTATION)
                        if not origin: continue
                        
                        match = ORIGIN_REGEX.match(origin)
                        if not match: continue
                        sup_ctx, origin_ns = match.group('sup_ctx'), match.group('ns')
                        
                        is_allowed = any(vt_sup['ctx'] == sup_ctx and (vt_sup['ns'] is None or vt_sup['ns'] == origin_ns) for vt_sup in valid_supervisor_targets if vt_sup['type'] == 'k8s')
                        if not is_allowed: continue

                        rel_path = os.path.relpath(file_path, local_path)
                        secret_path = f"git#{repo_url}/{rel_path}"
                        
                        all_clusters[secret_path] = {
                            'type': 'git',
                            'repo_url': repo_url,
                            'file_path': rel_path,
                            'local_repo_path': local_path,
                            'name': metadata.get('name'),
                            'origin': origin,
                            'hash': annotations.get(HASH_ANNOTATION)
                        }
                        log.info(f"ArgoCD Git secret found for CAPI {origin}: {secret_path}")
                        count += 1
            except Exception as e:
                pass
                
        log.info(f"Clusters ArgoCD (Git)[{SYNC_LABEL}=true] inside {vt['raw']}: {count}")
                
    return all_clusters


def existing_secret(cluster_path, dest_path, dest_type, argocd_clusters):
    for cl, data in argocd_clusters.items():
        if data['type'] == dest_type and data['origin'] == cluster_path:
            if dest_type == 'k8s' and cl.startswith(f"k8s#{dest_path}/"):
                return data
            elif dest_type == 'git' and cl == dest_path:
                return data
    return None


def upsert_k8s_secret(kube_manager, ctx_name, namespace, cluster_data, capi_cluster, existing_name=None):
    v1 = kube_manager.core_v1(ctx_name)
    
    metadata_args = {
        "labels": cluster_data.get('labels', {}),
        "annotations": cluster_data.get('annotations', {}),
        "namespace": namespace
    }
    
    if existing_name:
        metadata_args["name"] = existing_name
    else:
        metadata_args["generate_name"] = "cluster-"

    body = client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=client.V1ObjectMeta(**metadata_args),
        string_data={
            "name": cluster_data['name'],
            "server": cluster_data['server'],
            "config": cluster_data['config']
        }
    )

    if existing_name:
        v1.patch_namespaced_secret(name=existing_name, namespace=namespace, body=body)
        log.info(f"Updated ArgoCD K8s secret {ctx_name}://{namespace}/{existing_name} for CAPI {capi_cluster}")
        return f"k8s#{ctx_name}://{namespace}/{existing_name}"
    else:
        res = v1.create_namespaced_secret(namespace=namespace, body=body)
        log.info(f"Created ArgoCD K8s secret {ctx_name}://{namespace}/{res.metadata.name} for CAPI {capi_cluster}")
        return f"k8s#{ctx_name}://{namespace}/{res.metadata.name}"


def upsert_git_secret(repo_url, local_repo_path, file_path, cluster_data, capi_cluster):
    full_path = os.path.join(local_repo_path, file_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    action = "Updated" if os.path.exists(full_path) else "Created"
    
    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": cluster_data['name'],
            "labels": cluster_data.get('labels', {}),
            "annotations": cluster_data.get('annotations', {})
        },
        "type": "Opaque",
        "stringData": {
            "name": cluster_data['name'],
            "server": cluster_data['server'],
            "config": cluster_data['config']
        }
    }
    
    with open(full_path, 'w') as f:
        yaml.dump(secret_manifest, f, default_flow_style=False, sort_keys=False)
        
    log.info(f"{action} ArgoCD Git secret {repo_url}/{file_path} for CAPI {capi_cluster}")
    return f"git#{repo_url}/{file_path}"


def sync_argocd_secrets(kube_manager, capi_clusters, all_existing_secrets):
    active_secrets = []
    
    for capi_cluster_path, capi_data in capi_clusters.items():
        for destination in capi_data['destinations']:
            dest_type = destination['type']
            dest_path = destination['destinationPath']
            
            found_info = existing_secret(capi_cluster_path, dest_path, dest_type, all_existing_secrets)
            
            config_payload = json.dumps({
                "tlsClientConfig": {
                    "caData": capi_data['kubeconfig']['ca_data'],
                    "insecure": INSECURE,
                    "certData": capi_data['kubeconfig']['cert_data'],
                    "keyData": capi_data['kubeconfig']['key_data'],
                }
            })
            
            secret_name_value = f"{capi_data['context']}-{capi_data['namespace']}-{capi_data['name']}"
            server_value = capi_data['kubeconfig']['server']
            filtered_labels = capi_data.get('labels', {})
            
            raw_data = json.dumps({
                "name": secret_name_value,
                "server": server_value, 
                "config": config_payload,
                "labels": filtered_labels
            }, sort_keys=True).encode('utf-8')
            computed_hash = hashlib.sha256(raw_data).hexdigest()
            
            final_labels = {
                "argocd.argoproj.io/secret-type": "cluster",
                "managed-by": "argocd-sync",
                SYNC_LABEL: "true",
                **filtered_labels
            }
            
            payload = {
                "name": secret_name_value,
                "server": server_value, 
                "config": config_payload,
                "labels": final_labels,
                "annotations": {
                    ORIGIN_ANNOTATION: capi_cluster_path,
                    HASH_ANNOTATION: computed_hash
                }
            }
            
            if found_info and found_info.get('hash') == computed_hash:
                if dest_type == 'k8s':
                    full_log_path = f"{found_info['context']}://{found_info['namespace']}/{found_info['name']}"
                    active_secrets.append(f"k8s#{full_log_path}")
                else:
                    full_log_path = dest_path
                    active_secrets.append(dest_path)
                    
                log.info(f"Up-to-date ArgoCD Secret {full_log_path} for CAPI {capi_cluster_path}")
                continue
            
            if dest_type == 'k8s':
                target_name = found_info['name'] if found_info else None
                target_namespace = found_info['namespace'] if found_info else destination['namespace']
                target_ctx = found_info['context'] if found_info else destination['context']
                
                result_path = upsert_k8s_secret(kube_manager, target_ctx, target_namespace, payload, capi_cluster_path, target_name)
                active_secrets.append(result_path)
                
            elif dest_type == 'git':
                result_path = upsert_git_secret(destination['repo_url'], destination['local_repo_path'], destination['file_path'], payload, capi_cluster_path)
                active_secrets.append(result_path)
            
    return active_secrets


def cleanup_clusters(kube_manager, active_secrets, all_existing_secrets):
    to_clean = [fp for fp in all_existing_secrets if fp not in active_secrets]
    
    for fp in to_clean:
        info = all_existing_secrets[fp]
        origin = info['origin']
        
        if info['type'] == 'k8s':
            ctx, ns, name = info['context'], info['namespace'], info['name']
            try:
                kube_manager.core_v1(ctx).delete_namespaced_secret(name=name, namespace=ns)
                log.info(f"Cleanup (K8s): Removed orphan ArgoCD secret {ctx}://{ns}/{name} for CAPI {origin}")
            except Exception as e:
                log.error(f"Cleanup (K8s): Failed to delete {ctx}://{ns}/{name}: {e}")
                
        elif info['type'] == 'git':
            full_path = os.path.join(info['local_repo_path'], info['file_path'])
            try:
                if os.path.exists(full_path):
                    os.remove(full_path)
                    log.info(f"Cleanup (Git): Removed orphan YAML {info['repo_url']}/{info['file_path']} for CAPI {origin}")
            except Exception as e:
                log.error(f"Cleanup (Git): Failed to delete {full_path}: {e}")


def main():
    log.info(f"Syncing clusters, SUPERVISOR_CONTEXTS={SUPERVISOR_CONTEXTS}, ARGOCD_CONTEXTS={ARGOCD_CONTEXTS}")
    
    kube_manager = KubeManager()
    git_manager = GitManager()
    
    valid_argocd_targets = get_valid_targets(ARGOCD_CONTEXTS, kube_manager, "ArgoCD")
    valid_supervisor_targets = get_valid_targets(SUPERVISOR_CONTEXTS, kube_manager, "Supervisor")
    
    for vt in valid_argocd_targets:
        if vt['type'] == 'git':
            git_manager.prepare_repo(vt['repo'])
    
    capi_clusters = {}
    for vt in valid_supervisor_targets:
        if vt['type'] == 'k8s':
            clusters = kube_manager.get_capi_clusters(vt['raw'], valid_argocd_targets, git_manager)
            capi_clusters.update(clusters)
    
    all_existing_secrets = {}
    all_existing_secrets.update(get_all_argocd_clusters(kube_manager, valid_argocd_targets, valid_supervisor_targets))
    all_existing_secrets.update(get_all_git_clusters(git_manager, valid_argocd_targets, valid_supervisor_targets))
    
    active_secrets = sync_argocd_secrets(kube_manager, capi_clusters, all_existing_secrets)
    cleanup_clusters(kube_manager, active_secrets, all_existing_secrets)
    
    git_manager.commit_and_push_all()

if __name__ == "__main__":
    main()