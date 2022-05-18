import asyncio
import copy

from distributed.core import rpc

import kopf
import kubernetes_asyncio as kubernetes

from uuid import uuid4

from dask_kubernetes.common.auth import ClusterAuth
from dask_kubernetes.common.networking import (
    get_scheduler_address,
)


def build_scheduler_pod_spec(cluster_name, spec):
    scheduler_name = f"{cluster_name}-scheduler"
    pod_spec = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": scheduler_name,
            "labels": {
                "dask.org/cluster-name": cluster_name,
                "dask.org/component": "scheduler",
                "app": "scheduler",
                "version": "v1",
            },
        },
        "spec": spec,
    }

    pod_spec["spec"]["serviceAccountName"] = f"{scheduler_name}-service"

    return pod_spec


def build_scheduler_service_spec(cluster_name, spec):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{cluster_name}-scheduler-service",
            "labels": {
                "dask.org/cluster-name": cluster_name,
                "app": "scheduler",
                "service": "scheduler",
            },
        },
        "spec": spec,
    }


def build_scheduler_service_account_spec(cluster_name):
    scheduler_service_name = f"{cluster_name}-scheduler-service"
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": scheduler_service_name,
            "labels": {
                "dask.org/cluster-name": cluster_name,
                "account": scheduler_service_name,
            },
        },
    }


def build_worker_pod_spec(name, cluster_name, worker_name, spec):
    pod_spec = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": worker_name,
            "labels": {
                "dask.org/cluster-name": cluster_name,
                "dask.org/workergroup-name": name,
                "dask.org/component": "worker",
                "dask.org/worker-name": worker_name,
            },
        },
        "spec": copy.copy(spec),
    }

    pod_spec["spec"]["serviceAccountName"] = f"{worker_name}-service"
    pod_spec["spec"]["containers"][0]["env"].append(
        {"name": "DASK_WORKER_NAME", "value": worker_name}
    )

    return pod_spec


def build_worker_group_spec(name, spec):
    return {
        "apiVersion": "kubernetes.dask.org/v1",
        "kind": "DaskWorkerGroup",
        "metadata": {"name": f"{name}-default-worker-group"},
        "spec": {
            "cluster": name,
            "worker": spec,
        },
    }


def build_worker_service_spec(cluster_name, worker_name):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{worker_name}-service",
            "labels": {
                "dask.org/cluster-name": cluster_name,
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {
                "dask.org/cluster-name": cluster_name,
                "dask.org/worker-name": worker_name,
            },
            "ports": [
                {
                    "name": "comm",
                    "protocol": "TCP",
                    "port": 8788,
                    "targetPort": "comm",
                },
                {
                    "name": "dashboard",
                    "protocol": "TCP",
                    "port": 8787,
                    "targetPort": "dashboard",
                },
            ],
        },
    }


def build_worker_service_account_spec(cluster_name, worker_name):
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": f"{worker_name}-service",
            "labels": {
                "dask.org/cluster-name": cluster_name,
                "account": f"{worker_name}-service",
            },
        },
    }


def build_cluster_spec(name, worker_spec, scheduler_spec):
    return {
        "apiVersion": "kubernetes.dask.org/v1",
        "kind": "DaskCluster",
        "metadata": {"name": name},
        "spec": {"worker": worker_spec, "scheduler": scheduler_spec},
    }


async def wait_for_service(api, service_name, namespace):
    """Block until service is available."""
    while True:
        try:
            await api.read_namespaced_service(service_name, namespace)
            break
        except Exception:
            await asyncio.sleep(0.1)


@kopf.on.startup()
async def startup(**kwargs):
    await ClusterAuth.load_first()


@kopf.on.create("daskcluster")
async def daskcluster_create(spec, name, namespace, logger, **kwargs):
    logger.info(
        f"A DaskCluster has been created called {name} in {namespace} with the following config: {spec}"
    )
    async with kubernetes.client.api_client.ApiClient() as api_client:
        api = kubernetes.client.CoreV1Api(api_client)

        scheduler_service_account_spec = build_scheduler_service_account_spec(name)
        kopf.adopt(scheduler_service_account_spec)
        await api.create_namespaced_service_account(
            namespace=namespace,
            body=scheduler_service_account_spec,
        )

        # TODO Check for existing scheduler pod
        scheduler_spec = spec.get("scheduler", {})
        data = build_scheduler_pod_spec(name, scheduler_spec.get("spec"))
        kopf.adopt(data)
        await api.create_namespaced_pod(
            namespace=namespace,
            body=data,
        )
        # await wait_for_scheduler(name, namespace)
        logger.info(
            f"A scheduler pod has been created called {data['metadata']['name']} in {namespace} \
            with the following config: {data['spec']}"
        )

        # TODO Check for existing scheduler service
        data = build_scheduler_service_spec(name, scheduler_spec.get("service"))
        kopf.adopt(data)
        await api.create_namespaced_service(
            namespace=namespace,
            body=data,
        )
        await wait_for_service(api, data["metadata"]["name"], namespace)
        logger.info(
            f"A scheduler service has been created called {data['metadata']['name']} in {namespace} \
            with the following config: {data['spec']}"
        )

        worker_spec = spec.get("worker", {})
        data = build_worker_group_spec(name, worker_spec)
        # TODO: Next line is not needed if we can get worker groups adopted by the cluster
        kopf.adopt(data)
        api = kubernetes.client.CustomObjectsApi(api_client)
        await api.create_namespaced_custom_object(
            group="kubernetes.dask.org",
            version="v1",
            plural="daskworkergroups",
            namespace=namespace,
            body=data,
        )
        logger.info(
            f"A worker group has been created called {data['metadata']['name']} in {namespace} \
            with the following config: {data['spec']}"
        )


@kopf.on.create("daskworkergroup")
async def daskworkergroup_create(spec, name, namespace, logger, **kwargs):
    async with kubernetes.client.api_client.ApiClient() as api_client:
        api = kubernetes.client.CustomObjectsApi(api_client)
        cluster = await api.get_namespaced_custom_object(
            group="kubernetes.dask.org",
            version="v1",
            plural="daskclusters",
            namespace=namespace,
            name=spec["cluster"],
        )
        new_spec = dict(spec)
        kopf.adopt(new_spec, owner=cluster)
        api.api_client.set_default_header(
            "content-type", "application/merge-patch+json"
        )
        await api.patch_namespaced_custom_object(
            group="kubernetes.dask.org",
            version="v1",
            plural="daskworkergroups",
            namespace=namespace,
            name=name,
            body=new_spec,
        )
        logger.info(f"Successfully adopted by {spec['cluster']}")

    await daskworkergroup_update(
        spec=spec, name=name, namespace=namespace, logger=logger, **kwargs
    )


@kopf.on.update("daskworkergroup")
async def daskworkergroup_update(spec, name, namespace, logger, **kwargs):
    async with kubernetes.client.api_client.ApiClient() as api_client:
        api = kubernetes.client.CoreV1Api(api_client)

        workers = await api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"dask.org/workergroup-name={name}",
        )
        current_workers = len(workers.items)
        desired_workers = spec["worker"]["replicas"]
        workers_needed = desired_workers - current_workers

        if workers_needed > 0:
            for _ in range(workers_needed):
                worker_name = f"{name}-worker-{uuid4().hex[:10]}"

                worker_service_account_spec = build_worker_service_account_spec(
                    spec["cluster"], worker_name
                )
                kopf.adopt(worker_service_account_spec)
                await api.create_namespaced_service_account(
                    namespace=namespace,
                    body=worker_service_account_spec,
                )

                data = build_worker_service_spec(spec["cluster"], worker_name)
                kopf.adopt(data)
                await api.create_namespaced_service(
                    namespace=namespace,
                    body=data,
                )
                await wait_for_service(api, data["metadata"]["name"], namespace)
                data = build_worker_pod_spec(
                    name, spec["cluster"], worker_name, spec["worker"]["spec"]
                )
                kopf.adopt(data)
                await api.create_namespaced_pod(
                    namespace=namespace,
                    body=data,
                )
            logger.info(
                f"Scaled worker group {name} up to {spec['worker']['replicas']} workers."
            )
        if workers_needed < 0:
            service_name = f"{name.split('-')[0]}-cluster-service"
            address = await get_scheduler_address(service_name, namespace)
            async with rpc(address) as scheduler:
                worker_ids = await scheduler.workers_to_close(
                    n=-workers_needed, attribute="name"
                )
            # TODO: Check that were deting workers in the right worker group
            logger.info(f"Workers to close: {worker_ids}")
            for wid in worker_ids:
                await api.delete_namespaced_pod(
                    name=wid,
                    namespace=namespace,
                )
            logger.info(
                f"Scaled worker group {name} down to {spec['replicas']} workers."
            )
