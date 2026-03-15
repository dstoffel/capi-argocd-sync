import os
import yaml
import pytest
from unittest.mock import MagicMock, patch
from kubernetes import client, config

import capi_argocd_sync


class FakeCAPIState:
    def __init__(self):
        self.clusters = {
            "ctx1": [
                self._make_cluster("ctx1", "ctx1-ns1", "capi1", sync=True, extra_label=True),
                self._make_cluster("ctx1", "ctx1-ns1", "capi2", sync=True, extra_label=False),
                self._make_cluster("ctx1", "ctx1-ns2", "capi3", sync=False, extra_label=False),
            ],
            "ctx2": [
                self._make_cluster("ctx2", "ctx2-ns1", "capi1", sync=True, extra_label=True),
                self._make_cluster("ctx2", "ctx2-ns1", "capi2", sync=True, extra_label=False),
                self._make_cluster("ctx2", "ctx2-ns2", "capi3", sync=False, extra_label=False),
            ]
        }
        self.kubeconfig_hash = "v1"

    def _make_cluster(self, ctx, ns, name, sync, extra_label):
        labels = {}
        if sync:
            labels["argocd-sync/enabled"] = "true"
        if extra_label:
            labels["argocd-sync-label/env"] = "prod"
            
        return {
            "metadata": {
                "name": name,
                "namespace": ns,
                "labels": labels,
                "annotations": {}
            }
        }

    def get_custom_objects_mock(self, context):
        items = [c for c in self.clusters.get(context, []) if c["metadata"]["labels"].get("argocd-sync/enabled") == "true"]
        return {"items": items}

# --- 2. FIXTURES PYTEST ---

TEST_NAMESPACES = ["ctx1-ns1", "ctx1-ns2", "ctx2-ns1", "ctx2-ns2", "ns-xxx", "remote-ns"]

@pytest.fixture(scope="session")
def k8s_api():
    config.load_kube_config(context="minikube")
    return client.CoreV1Api()

@pytest.fixture(scope="session", autouse=True)
def manage_namespaces(k8s_api):
    for ns in TEST_NAMESPACES:
        try:
            k8s_api.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))
        except client.rest.ApiException:
            pass
            
    yield
    
    for ns in TEST_NAMESPACES:
        try:
            k8s_api.delete_namespace(name=ns)
        except client.rest.ApiException:
            pass

@pytest.fixture(autouse=True)
def clean_secrets_between_tests(k8s_api):
    for ns in TEST_NAMESPACES:
        secrets = k8s_api.list_namespaced_secret(namespace=ns, label_selector="managed-by=argocd-sync")
        for s in secrets.items:
            k8s_api.delete_namespaced_secret(name=s.metadata.name, namespace=ns)
    yield

@pytest.fixture
def rogue_secret(k8s_api):
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name="rogue-secret",
            namespace="ctx1-ns1",
            labels={"managed-by": "argocd-sync", "argocd-sync/enabled": "true"},
            annotations={"argocd-sync/origin": "unknown-ctx://ns/cluster"}
        ),
        string_data={"dummy": "data"}
    )
    k8s_api.create_namespaced_secret(namespace="ctx1-ns1", body=body)
    return "ctx1-ns1/rogue-secret"

@pytest.fixture
def mocked_env(monkeypatch, k8s_api, tmp_path):
    capi_state = FakeCAPIState()

    def mock_get_all_kube_clients(self):
        api_client = k8s_api.api_client
        # On ne renvoie plus que les clients d'API, on ne mock plus la liste des namespaces
        clients = {"ctx1": api_client, "ctx2": api_client, "remote-cluster": api_client, "in-cluster": api_client}
        return clients

    def mock_custom_objects(self, context):
        api = MagicMock()
        api.list_cluster_custom_object.side_effect = lambda **kwargs: capi_state.get_custom_objects_mock(context)
        return api

    def mock_extract_tls(secret_data):
        return f"https://api-{capi_state.kubeconfig_hash}", "ca", "cert", "key"

    # Patch des appels K8s
    monkeypatch.setattr(capi_argocd_sync.KubeManager, "_get_all_kube_clients", mock_get_all_kube_clients)
    monkeypatch.setattr(capi_argocd_sync.KubeManager, "custom_objects", mock_custom_objects)
    monkeypatch.setattr(capi_argocd_sync, "extract_tls_from_kubeconfig", mock_extract_tls)
    
    original_core_v1 = capi_argocd_sync.KubeManager.core_v1
    def mock_core_v1(self, context):
        real_api = original_core_v1(self, context)
        real_api.read_namespaced_secret = MagicMock(return_value=MagicMock(data={"value": "mocked"}))
        return real_api
    monkeypatch.setattr(capi_argocd_sync.KubeManager, "core_v1", mock_core_v1)

    # Patch des appels Git (on redirige le repo vers le dossier temporaire du test)
    monkeypatch.setattr(capi_argocd_sync.GitManager, "prepare_repo", lambda self, url: str(tmp_path))
    monkeypatch.setattr(capi_argocd_sync.GitManager, "get_repo_local_path", lambda self, url: str(tmp_path))
    monkeypatch.setattr(capi_argocd_sync.GitManager, "commit_and_push_all", lambda self: None) # Pas de vrai push

    return capi_state

# --- 3. LES TESTS END-TO-END KUBERNETES ---

def test_1_invalid_argocd_context(mocked_env, rogue_secret, k8s_api):
    capi_argocd_sync.SUPERVISOR_CONTEXTS = "ctx1://, ctx2://"
    capi_argocd_sync.ARGOCD_CONTEXTS = "ctx-unknown://"
    capi_argocd_sync.main()
    
    secrets = k8s_api.list_namespaced_secret(namespace="ctx1-ns1", label_selector="managed-by=argocd-sync").items
    assert len(secrets) == 1
    assert secrets[0].metadata.name == "rogue-secret"

def test_2_to_5_lifecycle(mocked_env, rogue_secret, k8s_api):
    capi_argocd_sync.SUPERVISOR_CONTEXTS = "ctx1://, ctx2://"
    capi_argocd_sync.ARGOCD_CONTEXTS = "ctx1://" 
    
    # --- TEST 2: CRÉATION ---
    capi_argocd_sync.main()
    secrets_ns1 = k8s_api.list_namespaced_secret(namespace="ctx1-ns1", label_selector="managed-by=argocd-sync").items
    assert len(secrets_ns1) == 3
    
    capi1_secret = next(s for s in secrets_ns1 if s.metadata.annotations and "ctx1://ctx1-ns1/capi1" == s.metadata.annotations.get("argocd-sync/origin"))
    assert capi1_secret.metadata.labels["argocd-sync-label/env"] == "prod"
    version_after_create = capi1_secret.metadata.resource_version

    # --- TEST 3: IDEMPOTENCE ---
    capi_argocd_sync.main()
    secrets_ns1_again = k8s_api.list_namespaced_secret(namespace="ctx1-ns1", label_selector="managed-by=argocd-sync").items
    capi1_secret_again = next(s for s in secrets_ns1_again if s.metadata.annotations and "ctx1://ctx1-ns1/capi1" == s.metadata.annotations.get("argocd-sync/origin"))
    assert capi1_secret_again.metadata.resource_version == version_after_create

    # --- TEST 4: SUPPRESSION DU LABEL ENABLED (GC) ---
    del mocked_env.clusters["ctx1"][1]["metadata"]["labels"]["argocd-sync/enabled"]
    capi_argocd_sync.main()
    secrets_ns1_gc = k8s_api.list_namespaced_secret(namespace="ctx1-ns1", label_selector="managed-by=argocd-sync").items
    assert len(secrets_ns1_gc) == 2 
    assert not any(s.metadata.annotations and "ctx1://ctx1-ns1/capi2" == s.metadata.annotations.get("argocd-sync/origin") for s in secrets_ns1_gc)
    assert any(s.metadata.name == "rogue-secret" for s in secrets_ns1_gc)

    # --- TEST 5: UPDATE LABEL & KUBECONFIG ---
    mocked_env.clusters["ctx1"][0]["metadata"]["labels"]["argocd-sync-label/env"] = "staging"
    mocked_env.kubeconfig_hash = "v2" 
    capi_argocd_sync.main()
    
    secrets_ns1_update = k8s_api.list_namespaced_secret(namespace="ctx1-ns1", label_selector="managed-by=argocd-sync").items
    capi1_secret_update = next(s for s in secrets_ns1_update if s.metadata.annotations and "ctx1://ctx1-ns1/capi1" == s.metadata.annotations.get("argocd-sync/origin"))
    
    assert capi1_secret_update.metadata.resource_version != version_after_create
    assert capi1_secret_update.metadata.labels["argocd-sync-label/env"] == "staging"


def test_6_destinations_routing(mocked_env, k8s_api):
    capi_argocd_sync.SUPERVISOR_CONTEXTS = "ctx1://"
    capi_argocd_sync.ARGOCD_CONTEXTS = "ctx1://ns-xxx, remote-cluster://remote-ns"
    capi_argocd_sync.INCLUSTER_NAME = "in-cluster"

    mocked_env.clusters["ctx1"][0]["metadata"]["annotations"]["argocd-sync/destinations"] = "in-cluster://ns-xxx"
    mocked_env.clusters["ctx1"][1]["metadata"]["annotations"]["argocd-sync/destinations"] = "remote-cluster://remote-ns"
    capi_argocd_sync.main()
    
    secrets_in_cluster = k8s_api.list_namespaced_secret(namespace="ns-xxx", label_selector="managed-by=argocd-sync").items
    assert len(secrets_in_cluster) == 1
    assert secrets_in_cluster[0].metadata.annotations["argocd-sync/origin"] == "ctx1://ctx1-ns1/capi1"

    secrets_remote = k8s_api.list_namespaced_secret(namespace="remote-ns", label_selector="managed-by=argocd-sync").items
    assert len(secrets_remote) == 1
    assert secrets_remote[0].metadata.annotations["argocd-sync/origin"] == "ctx1://ctx1-ns1/capi2"


# --- 4. LES TESTS END-TO-END GITOPS ---

def test_7_gitops_routing_and_security(mocked_env, tmp_path):
    """Vérifie l'écriture Git et la sécurité des chemins."""
    capi_argocd_sync.SUPERVISOR_CONTEXTS = "ctx1://"
    # On autorise uniquement le dossier 'allowed-path'
    capi_argocd_sync.ARGOCD_CONTEXTS = "git#https://github.com/org/repo.git/allowed-path"
    
    # CAPI 1 demande un chemin autorisé
    mocked_env.clusters["ctx1"][0]["metadata"]["annotations"]["argocd-sync/destinations"] = "git#https://github.com/org/repo.git/allowed-path/cluster1.yaml"
    # CAPI 2 demande un chemin INTERDIT (hors de allowed-path)
    mocked_env.clusters["ctx1"][1]["metadata"]["annotations"]["argocd-sync/destinations"] = "git#https://github.com/org/repo.git/hacked-path/cluster2.yaml"
    
    capi_argocd_sync.main()
    
    # Vérification: cluster1.yaml doit avoir été créé
    valid_file = tmp_path / "allowed-path" / "cluster1.yaml"
    assert valid_file.exists()
    
    with open(valid_file, 'r') as f:
        doc = yaml.safe_load(f)
        assert doc['metadata']['name'] == "ctx1-ctx1-ns1-capi1"
        assert doc['metadata']['labels']['argocd-sync-label/env'] == "prod"
        assert doc['metadata']['annotations']['argocd-sync/origin'] == "ctx1://ctx1-ns1/capi1"

    # Sécurité: cluster2.yaml ne doit pas exister
    invalid_file = tmp_path / "hacked-path" / "cluster2.yaml"
    assert not invalid_file.exists()


def test_8_gitops_lifecycle(mocked_env, tmp_path):
    """Vérifie la mise à jour (idempotence) et la suppression Git."""
    capi_argocd_sync.SUPERVISOR_CONTEXTS = "ctx1://"
    capi_argocd_sync.ARGOCD_CONTEXTS = "git#https://github.com/org/repo.git/clusters"
    mocked_env.clusters["ctx1"][0]["metadata"]["annotations"]["argocd-sync/destinations"] = "git#https://github.com/org/repo.git/clusters/c1.yaml"
    
    # CRÉATION INITIALE
    capi_argocd_sync.main()
    target_file = tmp_path / "clusters" / "c1.yaml"
    assert target_file.exists()
    
    # RÉCUPÉRATION DE L'HEURE DE MODIFICATION (pour l'idempotence)
    mtime_initial = os.path.getmtime(target_file)
    
    # IDEMPOTENCE: on relance sans rien changer
    capi_argocd_sync.main()
    mtime_after = os.path.getmtime(target_file)
    assert mtime_initial == mtime_after # Le fichier n'a pas été réécrit
    
    # UPDATE: on change le label
    mocked_env.clusters["ctx1"][0]["metadata"]["labels"]["argocd-sync-label/env"] = "staging"
    capi_argocd_sync.main()
    with open(target_file, 'r') as f:
        doc = yaml.safe_load(f)
        assert doc['metadata']['labels']['argocd-sync-label/env'] == "staging" # Le label a été mis à jour
        
    # GARBAGE COLLECTION: on retire l'autorisation
    del mocked_env.clusters["ctx1"][0]["metadata"]["labels"]["argocd-sync/enabled"]
    capi_argocd_sync.main()
    
    # Le fichier doit avoir été supprimé par le script
    assert not target_file.exists()