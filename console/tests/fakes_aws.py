"""AWS test doubles: the shared FakeK8s (+ an Ingress view) and real boto3 clients under moto's mock_aws;
CloudWatch Logs
Insights is a hand fake because moto does not evaluate Insights queries."""

import os
from contextlib import contextmanager

from tests.fakes_gcp import FakeK8s

REGION = "us-east-1"
ACCOUNT = "123456789012"  # moto's fixed account id
BUCKET = f"ramen-{ACCOUNT}-groups"


class _Networking:
    def __init__(self, s):
        self.s = s

    def create_namespaced_ingress(self, ns, body):
        return self.s._create("Ingress", ns, body)

    def patch_namespaced_ingress(self, name, ns, body):
        return self.s._patch("Ingress", ns, name, body)

    def read_namespaced_ingress(self, name, ns):
        return self.s._read("Ingress", ns, name)

    def create_namespaced_network_policy(self, ns, body):
        return self.s._create("NetworkPolicy", ns, body)

    def patch_namespaced_network_policy(self, name, ns, body):
        return self.s._patch("NetworkPolicy", ns, name, body)

    def read_namespaced_network_policy(self, name, ns):
        return self.s._read("NetworkPolicy", ns, name)


class FakeLogsInsights:
    """results: list of {field: value} rows; `pending` = polls answering Running first; `fail` = final status."""

    def __init__(self, results=None, pending=0, fail=None, missing=False):
        self.results, self.pending, self.fail, self.missing = results or [], pending, fail, missing
        self.queries: list[dict] = []
        self.stopped: list[str] = []

    def start_query(self, **kw):
        if self.missing:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no such log group"}}, "StartQuery"
            )
        self.queries.append(kw)
        return {"queryId": f"q{len(self.queries)}"}

    def get_query_results(self, queryId):
        if self.pending > 0:
            self.pending -= 1
            return {"status": "Running", "results": []}
        if self.fail:
            return {"status": self.fail, "results": []}
        return {
            "status": "Complete",
            "results": [[{"field": k, "value": v} for k, v in row.items()] for row in self.results],
        }

    def stop_query(self, queryId):
        self.stopped.append(queryId)
        return {"success": True}


@contextmanager
def aws_env():
    """moto sandbox + fake credentials; boto3 clients created inside are served by moto."""
    from moto import mock_aws

    old = {
        k: os.environ.get(k)
        for k in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_DEFAULT_REGION",
            "AWS_SECURITY_TOKEN",
            "AWS_SESSION_TOKEN",
        )
    }
    os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION=REGION)
    try:
        with mock_aws():
            yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeAwsClients:
    """Same attribute names as ramen_console.cloud.aws_api.AwsClients. Call `seed()` inside aws_env()."""

    def __init__(self, k8s=None, logs=None, region=REGION):
        self.k8s = k8s or FakeK8s()
        self.logs = logs or FakeLogsInsights()
        self.region = region
        self._boto: dict = {}

    def seed(self, cluster="ramen", bucket=BUCKET):
        self.s3.create_bucket(Bucket=bucket)
        self.eks.create_cluster(
            name=cluster, roleArn=f"arn:aws:iam::{ACCOUNT}:role/eks", resourcesVpcConfig={"subnetIds": ["subnet-1"]}
        )
        return self

    def alb(self, group="ramen"):
        """Create an ALB tagged like the Load Balancer Controller would for IngressGroup `group`."""
        ec2 = self._client("ec2")
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        subnets = [
            ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=f"{self.region}{az}")["Subnet"][
                "SubnetId"
            ]
            for i, az in ((1, "a"), (2, "b"))
        ]
        lb = self.elbv2.create_load_balancer(
            Name=f"k8s-{group}-abc",
            Subnets=subnets,
            Tags=[{"Key": "ingress.k8s.aws/stack", "Value": group}, {"Key": "elbv2.k8s.aws/cluster", "Value": "ramen"}],
        )
        return lb["LoadBalancers"][0]

    def _client(self, name):
        if name not in self._boto:
            import boto3

            self._boto[name] = boto3.client(name, region_name=self.region)
        return self._boto[name]

    s3 = property(lambda self: self._client("s3"))
    secretsmanager = property(lambda self: self._client("secretsmanager"))
    wafv2 = property(lambda self: self._client("wafv2"))
    elbv2 = property(lambda self: self._client("elbv2"))
    iam = property(lambda self: self._client("iam"))
    sts = property(lambda self: self._client("sts"))
    eks = property(lambda self: self._client("eks"))

    @property
    def core(self):
        return self.k8s.core

    @property
    def apps(self):
        return self.k8s.apps

    @property
    def autoscaling(self):
        return self.k8s.autoscaling

    @property
    def custom(self):
        return self.k8s.custom

    @property
    def networking(self):
        return _Networking(self.k8s)

    def to_dict(self, obj):
        return obj


def web_acl(wafv2, name="ramen") -> dict | None:
    for s in wafv2.list_web_acls(Scope="REGIONAL")["WebACLs"]:
        if s["Name"] == name:
            return wafv2.get_web_acl(Name=name, Scope="REGIONAL", Id=s["Id"])["WebACL"]
    return None


def ip_set(wafv2, name) -> dict | None:
    for s in wafv2.list_ip_sets(Scope="REGIONAL")["IPSets"]:
        if s["Name"] == name:
            return wafv2.get_ip_set(Name=name, Scope="REGIONAL", Id=s["Id"])["IPSet"]
    return None
