"""An explicitly limited contact grant must not become an unrestricted route."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from mail_transport import AgentMail, configure_mail
from workbook import Workbook, WorkbookError, configure, now, read


class ScopedRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-scoped-route-")
        self.base = Path(self.temp.name).resolve()
        self.root, self.store = self.base/"workbooks", self.base/"mail"
        self.config = {"manager":"manager", "agents":{x:{"role":x} for x in ["manager","ui","admin","stranger"]},
                       "allowed_routes":[["manager","ui"],["ui","manager"],["manager","admin"],["admin","manager"]]}
        configure(self.root,self.config)
        for actor in self.config["agents"]: Workbook(self.root,actor).init()
        configure_mail(self.store,self.root,"manager")
        self.grant={"grant_id":"ROUTE-R06", "sender":"ui", "recipient":"admin", "task_ids":["TASK-UI-R06"],
                    "message_types":["COORDINATION_REQUEST"], "work_item_ids":["WORK-ADMIN-R06"],
                    "status":"active", "authorization_ref":"fixed owner request for this dependency only"}
        self.config["task_scoped_routes"]=[self.grant]
        configure(self.root,self.config,"manager")

    def tearDown(self):
        self.assertEqual(self.base.parent,Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.base.name.startswith("agent-scoped-route-"))
        self.temp.cleanup()

    def message(self):
        return {"message_id":"MSG-R06", "sender":"ui", "recipient":"admin", "work_item_id":"WORK-ADMIN-R06",
                "task_id":"TASK-UI-R06", "type":"COORDINATION_REQUEST", "summary":"fixed manual dependency",
                "next_action":"process only the fixed shared-panel reference", "requires_action":True,
                "authorization_ref":"fixed task owner authorization", "created_at":now()}

    def test_exact_mail_can_be_opened_and_self_queued_without_global_pair(self):
        AgentMail(self.store,self.root,"ui").send(self.message())
        recipient=AgentMail(self.store,self.root,"admin")
        recipient.open("ui","MSG-R06");recipient.accept("ui","MSG-R06")
        self.assertEqual(Workbook(self.root,"admin").claim("MSG-R06")["status"],"claimed")
        self.assertNotIn(["ui","admin"],read(self.root/"registry.json")["allowed_routes"])

    def test_wrong_task_type_work_item_direction_or_recipient_is_rejected(self):
        for field,value in [("task_id","TASK-OTHER"),("type","ASSIGN"),("work_item_id","WORK-OTHER"),("recipient","stranger")]:
            with self.assertRaises(WorkbookError): AgentMail(self.store,self.root,"ui").send({**self.message(),field:value})
        with self.assertRaises(WorkbookError):
            AgentMail(self.store,self.root,"admin").send({**self.message(),"sender":"admin","recipient":"ui"})
        self.assertFalse(list((self.store/"messages").rglob("*.json")))

    def test_same_client_observes_revocation_and_preserves_existing_mail(self):
        sender=AgentMail(self.store,self.root,"ui");recipient=AgentMail(self.store,self.root,"admin")
        sender.send(self.message());obj=self.store/"messages/ui/MSG-R06.json";before=obj.read_bytes()
        recipient.open("ui","MSG-R06");recipient.accept("ui","MSG-R06")
        updated=deepcopy(self.config);updated["task_scoped_routes"][0]["status"]="revoked"
        configure(self.root,updated,"manager")
        with self.assertRaises(WorkbookError):sender.send({**self.message(),"message_id":"MSG-R06-2"})
        with self.assertRaises(WorkbookError):recipient.open("ui","MSG-R06")
        with self.assertRaises(WorkbookError):Workbook(self.root,"admin").claim("MSG-R06")
        self.assertEqual(Workbook(self.root,"admin").state()["queued"]["MSG-R06"]["status"],"queued")
        self.assertEqual(before,obj.read_bytes())

    def test_ambiguous_empty_or_wildcard_grants_are_not_written(self):
        before=(self.root/"registry.json").read_bytes()
        candidates=[]
        for field,value in [("task_ids",[]),("message_types",["*"]),("work_item_ids",[]),("authorization_ref","")]:
            c=deepcopy(self.config);c["task_scoped_routes"][0][field]=value;candidates.append(c)
        c=deepcopy(self.config);c["allowed_routes"].append(["ui","admin"]);candidates.append(c)
        c=deepcopy(self.config);c["task_scoped_routes"].append(deepcopy(self.grant));candidates.append(c)
        for c in candidates:
            with self.assertRaises(WorkbookError):configure(self.root,c,"manager")
            self.assertEqual(before,(self.root/"registry.json").read_bytes())
        changed=deepcopy(self.config);changed["task_scoped_routes"][0]["status"]="revoked"
        with self.assertRaises(WorkbookError):configure(self.root,changed,"ui")

    def test_scoped_grant_cannot_publish_an_undiscoverable_legacy_message(self):
        with self.assertRaisesRegex(WorkbookError,"AgentMail.send"):
            Workbook(self.root,"ui").send(self.message())
        self.assertFalse(list((self.root/"agents/ui/outbox").rglob("*.json")))

    def test_revoked_completed_mail_does_not_block_another_valid_claim(self):
        AgentMail(self.store,self.root,"ui").send(self.message())
        recipient=AgentMail(self.store,self.root,"admin")
        recipient.open("ui","MSG-R06");recipient.accept("ui","MSG-R06")
        book=Workbook(self.root,"admin");book.claim("MSG-R06");book.transition("WORK-ADMIN-R06","completed","fixture done")
        c=deepcopy(self.config);c["task_scoped_routes"][0]["status"]="revoked";configure(self.root,c,"manager")
        other={**self.message(),"message_id":"MSG-GLOBAL","sender":"manager","task_id":"TASK-GLOBAL","work_item_id":"WORK-GLOBAL","type":"ASSIGN"}
        AgentMail(self.store,self.root,"manager").send(other)
        recipient.open("manager","MSG-GLOBAL");recipient.accept("manager","MSG-GLOBAL")
        self.assertEqual(book.claim("MSG-GLOBAL")["status"],"claimed")
        self.assertEqual(book.state()["items"]["WORK-ADMIN-R06"]["status"],"completed")


if __name__=="__main__":unittest.main()
