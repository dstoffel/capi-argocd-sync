import base64
import json
import hashlib
import os
import pytest
from unittest.mock import MagicMock, patch, mock_open

import capi_argocd_sync

def generate_fake_kubeconfig_b64():
    kubeconfig_dict = {
        "clusters": [{"cluster": {"server": "https://api.fake.local", "certificate-authority-data": "Y2EtZGF0YQ=="}}],
        "users": [{"user": {"client-certificate-data": "Y2VydC1kYXRh", "client-key-data": "a2V5LWRhdGE="}}]
    }
    import yaml
    yaml_str = yaml.dump(kubeconfig_dict)
    return base64.b64encode(yaml_str.encode('utf-8')).decode('utf-8')

@pytest.fixture
def mock_kube_manager():
    km = MagicMock()
    # On ne mock plus les namespaces, seulement les clients
    km.clients = {"cluster-a": MagicMock(), "cluster-b": MagicMock()}
    return km

def test_get_valid_targets(mock_kube_manager):
    """Teste le parsing des cibles K8s et Git."""
    env_value = "cluster-a://argocd, git#https://github.com/org/repo.git/my-path"
    
    targets = capi_argocd_sync.get_valid_targets(env_value, mock_kube_manager, "Test")
    
    assert len(targets) == 2
    assert targets[0] == {'type': 'k8s', 'raw': 'cluster-a://argocd', 'ctx': 'cluster-a', 'ns': 'argocd'}
    assert targets[1] == {'type': 'git', 'raw': 'git#https://github.com/org/repo.git/my-path', 'repo': 'https://github.com/org/repo.git', 'path': 'my-path'}

@patch('capi_argocd_sync.KubeManager._get_all_kube_clients')
@patch('capi_argocd_sync.KubeManager.custom_objects')
@patch('capi_argocd_sync.KubeManager.core_v1')
def test_get_capi_clusters_with_labels_and_git(mock_core_v1, mock_custom_objects, mock_get_clients):
    """Teste la lecture CAPI, le filtrage des labels, et la validation Git/K8s."""
    # On retourne uniquement le dictionnaire des clients, plus le tuple
    mock_get_clients.return_value = {"sup-cluster": MagicMock(), "local-cluster": MagicMock()}
    
    mock_custom_api = MagicMock()
    mock_custom_objects.return_value = mock_custom_api
    
    fake_cluster = {
        "metadata": {
            "name": "workload-1",
            "namespace": "capi-system",
            "labels": {
                "argocd-sync-label/env": "prod",  # Doit être gardé
                "ignore-this-label": "true"       # Doit être supprimé
            },
            "annotations": {
                "argocd-sync/destinations": "local-cluster://argocd, git#https://github.com/org/repo.git/clusters/secret.yaml"
            }
        }
    }
    mock_custom_api.list_namespaced_custom_object.return_value = {"items": [fake_cluster]}

    mock_core_api = MagicMock()
    mock_core_v1.return_value = mock_core_api
    mock_secret = MagicMock()
    mock_secret.data = {"value": generate_fake_kubeconfig_b64()}
    mock_core_api.read_namespaced_secret.return_value = mock_secret

    km = capi_argocd_sync.KubeManager()
    gm = MagicMock()
    gm.prepare_repo.return_value = "/tmp/fake-git-cache"

    valid_argocd_targets = [
        {'type': 'k8s', 'ctx': 'local-cluster', 'ns': 'argocd'},
        {'type': 'git', 'repo': 'https://github.com/org/repo.git', 'path': 'clusters'}
    ]

    clusters = km.get_capi_clusters("sup-cluster://capi-system", valid_argocd_targets, gm)

    expected_path = "sup-cluster://capi-system/workload-1"
    assert expected_path in clusters
    cc = clusters[expected_path]
    
    # Vérification du filtrage des labels
    assert "argocd-sync-label/env" in cc['labels']
    assert "ignore-this-label" not in cc['labels']
    
    # Vérification des destinations mixtes (K8s + Git)
    assert len(cc['destinations']) == 2
    types = [d['type'] for d in cc['destinations']]
    assert 'k8s' in types
    assert 'git' in types

@patch('builtins.open', new_callable=mock_open)
@patch('os.makedirs')
@patch('os.path.exists', return_value=False)
@patch('capi_argocd_sync.KubeManager._get_all_kube_clients')
@patch('capi_argocd_sync.KubeManager.core_v1')
def test_sync_argocd_secrets_mixed_routing(mock_core_v1, mock_get_clients, mock_exists, mock_makedirs, mock_file):
    """Teste que l'upsert route correctement vers K8s (API) et Git (Fichier local)."""
    # Renvoie un dictionnaire vide pour les clients
    mock_get_clients.return_value = {}
    
    mock_api = MagicMock()
    mock_core_v1.return_value = mock_api
    mock_created_secret = MagicMock()
    mock_created_secret.metadata.name = "cluster-generated123"
    mock_api.create_namespaced_secret.return_value = mock_created_secret

    km = capi_argocd_sync.KubeManager()
    
    capi_clusters = {
        "sup-cluster://capi-system/workload-1": {
            "name": "workload-1",
            "namespace": "capi-system",
            "context": "sup-cluster",
            "kubeconfig": {"server": "https://api", "ca_data": "ca", "cert_data": "cert", "key_data": "key"},
            "labels": {"argocd-sync-label/env": "test"},
            "destinations": [
                {"type": "k8s", "destinationPath": "local-cluster://argocd", "context": "local-cluster", "namespace": "argocd"},
                {"type": "git", "destinationPath": "git#https://github.com/org/repo.git/path/sec.yaml", "repo_url": "https://github.com/org/repo.git", "file_path": "path/sec.yaml", "local_repo_path": "/tmp/repo"}
            ]
        }
    }
    
    argocd_clusters = {} # Aucun existant, on force la création
    active_secrets = capi_argocd_sync.sync_argocd_secrets(km, capi_clusters, argocd_clusters)
    
    # Vérification K8s
    mock_api.create_namespaced_secret.assert_called_once()
    assert "k8s#local-cluster://argocd/cluster-generated123" in active_secrets
    
    # Vérification Git
    mock_file.assert_called_once_with("/tmp/repo/path/sec.yaml", 'w')
    assert "git#https://github.com/org/repo.git/path/sec.yaml" in active_secrets

@patch('capi_argocd_sync.KubeManager._get_all_kube_clients')
def test_sync_argocd_secrets_idempotence(mock_get_clients):
    """Teste que la mise à jour est ignorée si le hash correspond (en incluant les labels)."""
    # Renvoie un dictionnaire vide pour les clients
    mock_get_clients.return_value = {}
    km = capi_argocd_sync.KubeManager()
    
    server_url = "https://api-unchanged"
    ca_val, cert_val, key_val = "ca", "cert", "key"
    capi_cluster_path = "sup-cluster://capi-system/workload-1"
    labels = {"argocd-sync-label/env": "prod"}
    
    capi_clusters = {
        capi_cluster_path: {
            "name": "workload-1",
            "namespace": "capi-system",
            "context": "sup-cluster",
            "kubeconfig": {"server": server_url, "ca_data": ca_val, "cert_data": cert_val, "key_data": key_val},
            "labels": labels,
            "destinations": [{"type": "k8s", "destinationPath": "local-cluster://argocd", "context": "local-cluster", "namespace": "argocd"}]
        }
    }
    
    config_payload = json.dumps({"tlsClientConfig": {"caData": ca_val, "insecure": capi_argocd_sync.INSECURE, "certData": cert_val, "keyData": key_val}})
    
    raw_data = json.dumps({
        "name": "sup-cluster-capi-system-workload-1",
        "server": server_url,
        "config": config_payload,
        "labels": labels 
    }, sort_keys=True).encode('utf-8')
    
    expected_hash = hashlib.sha256(raw_data).hexdigest()

    argocd_clusters = {
        "k8s#local-cluster://argocd/existing-secret": {
            "type": "k8s",
            "context": "local-cluster",
            "namespace": "argocd",
            "name": "existing-secret",
            "origin": capi_cluster_path,
            "hash": expected_hash 
        }
    }

    with patch.object(km, 'core_v1') as mock_core_v1:
        mock_api = MagicMock()
        mock_core_v1.return_value = mock_api
        
        active_secrets = capi_argocd_sync.sync_argocd_secrets(km, capi_clusters, argocd_clusters)
        
        # Aucun appel API autorisé car le hash match !
        mock_api.patch_namespaced_secret.assert_not_called()
        mock_api.create_namespaced_secret.assert_not_called()
        
        # Le chemin complet doit être préservé
        assert "k8s#local-cluster://argocd/existing-secret" in active_secrets

@patch('os.remove')
@patch('os.path.exists', return_value=True)
@patch('capi_argocd_sync.KubeManager._get_all_kube_clients')
@patch('capi_argocd_sync.KubeManager.core_v1')
def test_cleanup_mixed_clusters(mock_core_v1, mock_get_clients, mock_exists, mock_remove):
    """Teste le Garbage Collector pour Kubernetes ET Git."""
    # Renvoie un dictionnaire vide pour les clients
    mock_get_clients.return_value = {}
    
    mock_api = MagicMock()
    mock_core_v1.return_value = mock_api

    km = capi_argocd_sync.KubeManager()

    active_secrets = ["k8s#local-cluster://argocd/valid-secret"]
    
    all_existing_secrets = {
        "k8s#local-cluster://argocd/valid-secret": {
            "type": "k8s",
            "context": "local-cluster",
            "namespace": "argocd",
            "name": "valid-secret",
            "origin": "sup-cluster://capi-system/workload-1"
        },
        "k8s#local-cluster://argocd/orphan-k8s": {
            "type": "k8s",
            "context": "local-cluster",
            "namespace": "argocd",
            "name": "orphan-k8s",
            "origin": "sup-cluster://capi-system/deleted-workload"
        },
        "git#https://github.com/org/repo.git/path/orphan-git.yaml": {
            "type": "git",
            "repo_url": "https://github.com/org/repo.git",
            "file_path": "path/orphan-git.yaml",
            "local_repo_path": "/tmp/repo",
            "origin": "sup-cluster://capi-system/deleted-workload"
        }
    }

    capi_argocd_sync.cleanup_clusters(km, active_secrets, all_existing_secrets)

    # 1. Vérifie la suppression K8s
    mock_api.delete_namespaced_secret.assert_called_once_with(name="orphan-k8s", namespace="argocd")
    
    # 2. Vérifie la suppression du fichier local Git
    mock_remove.assert_called_once_with("/tmp/repo/path/orphan-git.yaml")