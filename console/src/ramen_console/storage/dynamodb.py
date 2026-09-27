import asyncio
import json
import os

from boto3.dynamodb.conditions import Key

from .base import Doc, Filters, Store, matches


class DynamoStore(Store):
    def __init__(self, table: str, resource):
        self._t = resource.Table(table)
        self._name, self._res = table, resource

    @classmethod
    def from_env(cls):
        import boto3
        kw = {}
        if os.environ.get("RAMEN_DDB_ENDPOINT"):
            kw["endpoint_url"] = os.environ["RAMEN_DDB_ENDPOINT"]
        s = cls(os.environ.get("RAMEN_DDB_TABLE", "ramen"), boto3.resource("dynamodb", **kw))
        s.ensure_table()
        return s

    def ensure_table(self):
        names = [t.name for t in self._res.tables.all()]
        if self._name in names:
            return
        t = self._res.create_table(
            TableName=self._name,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t.wait_until_exists()

    async def get(self, collection, key):
        r = await asyncio.to_thread(self._t.get_item, Key={"pk": collection, "sk": key})
        item = r.get("Item")
        return json.loads(item["doc"]) if item else None

    async def put(self, collection, key, doc: Doc):
        stored = {**doc, "id": key}
        await asyncio.to_thread(self._t.put_item, Item={"pk": collection, "sk": key, "doc": json.dumps(stored)})
        return dict(stored)

    async def delete(self, collection, key):
        await asyncio.to_thread(self._t.delete_item, Key={"pk": collection, "sk": key})

    async def list(self, collection, filters: Filters = None):
        r = await asyncio.to_thread(self._t.query, KeyConditionExpression=Key("pk").eq(collection))
        docs = [json.loads(i["doc"]) for i in r.get("Items", [])]
        return [d for d in docs if matches(d, filters)]
