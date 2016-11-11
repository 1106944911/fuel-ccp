from __future__ import print_function

import logging

from fuel_ccp import config
from fuel_ccp import kubernetes

CONF = config.CONF

LOG = logging.getLogger(__name__)


class State(object):
    def __init__(self, name, total, running, urls):
        self.name = name
        self.total = total or 0
        self.running = running or 0
        self.job_completed = 0
        self.job_total = 0
        self.urls = urls or []

    def __repr__(self):
        if self.ready:
            return "ok"
        else:
            return "nok"  # not ready

    def __lt__(self, other):
        return self.name.__lt__(other.name)

    def __bool__(self):
        return self.ready

    def __nonzero__(self):
        return self.ready

    @property
    def ready(self):
        return (self.total == self.running
                and self.job_total == self.job_completed)


def _get_pods_status(service, svc_map):
    pods = kubernetes.list_cluster_pods(service=service)
    total = running = 0
    for pod in pods:
        total += 1
        if pod.ready:
            running += 1
    return State(
        name=service,
        total=total,
        running=running,
        urls=svc_map.get(service, []))


def get_pod_states(components=None):
    ext_ip = CONF.configs.get("k8s_external_ip", "")
    ext_link_template = "http://{ext_ip}:{port}"
    states = []
    svc_map = {}
    for svc in kubernetes.list_cluster_services():
        svc_name = svc.obj["metadata"]["name"]
        svc_map.setdefault(svc_name, [])
        for port in svc.obj["spec"]["ports"]:
            svc_map[svc_name].append(ext_link_template.format(
                ext_ip=ext_ip,
                port=port["nodePort"]))
    for dp in kubernetes.list_cluster_deployments():
        if not components or dp.name in components:
            states.append(_get_pods_status(dp.name, svc_map))

    job_states = {}
    for job in kubernetes.list_cluster_jobs():
        service = job.obj["metadata"]["labels"].get("app")
        job_states.setdefault(service, {"completed": 0, "total": 0})
        job_states[service]["total"] += job.obj["spec"]["completions"]
        job_states[service]["completed"] += job.obj["status"].get("succeeded",
                                                                  0)
    for state in states:
        if state.name in job_states:
            state.job_total = job_states[state.name]["total"]
            state.job_completed = job_states[state.name]["completed"]

    return states


def show_long_status(components=None):
    states = get_pod_states(components)
    columns = ("service", "pod", "job", "ready", "links")

    formatted_states = []

    for state in sorted(states):
        formatted_states.append((
            state.name,
            "%d/%d" % (state.running, state.total),
            "%d/%d" % (state.job_completed, state.job_total),
            state,
            "\n".join(state.urls)))

    return columns, formatted_states


def show_short_status():
    status = "ok" if all(get_pod_states()) else "nok"
    return ("status",), ((status,),)
