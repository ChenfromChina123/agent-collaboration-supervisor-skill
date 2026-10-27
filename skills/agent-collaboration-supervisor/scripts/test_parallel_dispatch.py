"""Controlled filesystem fixtures, not actual product assignment or Agent spawning."""
import json
from pathlib import Path
import tempfile
import unittest

from workbook import Workbook, configure, now


class ParallelDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="parallel-dispatch-test-")
        self.root=Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent,Path(tempfile.gettempdir()).resolve())
        configure(self.root,{"manager":"product","agents":{x:{"role":x} for x in ("product","dispatcher","developer")},"allowed_routes":[["product","product"],["product","dispatcher"],["dispatcher","product"],["dispatcher","developer"],["developer","dispatcher"]]})
        self.books={x:Workbook(self.root,x) for x in ("product","dispatcher","developer")}
        for book in self.books.values():book.init()

    def tearDown(self):
        self.assertEqual(self.root.parent,Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("parallel-dispatch-test-"))
        self.temp.cleanup()

    def message(self,key,sender,recipient,kind="TASK"):
        return {"message_id":"MSG-"+key,"work_item_id":"WORK-"+key,"task_id":"FIXTURE-TASK-"+key,"sender":sender,"recipient":recipient,"type":kind,"summary":"controlled approved local workflow fixture","next_action":"operate only on this isolated fixture","authorization_ref":"explicit local fixture, no product/server authorization","requires_action":True,"created_at":now()}

    def keep_product_busy(self):
        item=self.message("PRODUCT-ANALYSIS","product","product")
        book=self.books["product"];book.send(item);book.scan();book.claim(item["message_id"]);book.transition(item["work_item_id"],"in_progress","long product analysis fixture")
        return item

    def test_dispatch_ready_while_product_is_busy_without_touching_product_book(self):
        card=self.message("READY","product","dispatcher","READY_FOR_DISPATCH")
        self.books["product"].send(card)
        active=self.keep_product_busy()
        pm=self.books["product"]
        before={str(p.relative_to(pm.directory)):p.read_bytes() for p in pm.directory.rglob("*") if p.is_file()}
        dispatch=self.books["dispatcher"];dispatch.scan();dispatch.claim(card["message_id"])
        dispatch.transition(card["work_item_id"],"in_progress","fixture readiness already approved; owner/capacity verified")
        assignment=self.message("ASSIGN-READY","dispatcher","developer","ASSIGN")
        assignment["task_id"]=card["task_id"]
        assignment["authorization_ref"]=card["message_id"]
        dispatch.send(assignment)
        dispatch.send(assignment)
        self.assertEqual(len(dispatch.state()["outbox"]),1)
        dispatch.transition(card["work_item_id"],"completed","assignment durably published, developer not yet received")
        after={str(p.relative_to(pm.directory)):p.read_bytes() for p in pm.directory.rglob("*") if p.is_file()}
        self.assertEqual(before,after)
        self.assertEqual(pm.state()["active_work_item"],active["work_item_id"])
        self.assertEqual(dispatch.receipts()[0]["status"],"awaiting_recipient_scan")
        developer=self.books["developer"];developer.scan();developer.claim(assignment["message_id"])
        self.assertEqual(developer.state()["active_work_item"],assignment["work_item_id"])
        self.assertEqual(developer.state()["queued"][assignment["message_id"]]["task_id"],card["task_id"])

    def test_blocked_product_question_does_not_block_independent_ready_dispatch(self):
        unclear=self.message("UNREADY","product","dispatcher","NEEDS_PRODUCT_DECISION")
        ready=self.message("SECOND-READY","product","dispatcher","READY_FOR_DISPATCH")
        self.books["product"].send(unclear);self.books["product"].send(ready)
        active=self.keep_product_busy()
        dispatch=self.books["dispatcher"];dispatch.scan();dispatch.claim(unclear["message_id"])
        dispatch.transition(unclear["work_item_id"],"blocked","missing project/owner; product decision required")
        question=self.message("CLARIFY","dispatcher","product","PRODUCT_QUERY");dispatch.send(question)
        dispatch.scan();dispatch.claim(ready["message_id"])
        dispatch.transition(ready["work_item_id"],"in_progress","independent ready item continues")
        assignment=self.message("ASSIGN-SECOND","dispatcher","developer","ASSIGN")
        assignment["task_id"]=ready["task_id"]
        assignment["authorization_ref"]=ready["message_id"]
        dispatch.send(assignment)
        state=dispatch.state()
        self.assertEqual(state["items"][unclear["work_item_id"]]["status"],"blocked")
        self.assertEqual(state["active_work_item"],ready["work_item_id"])
        self.assertEqual(self.books["product"].state()["active_work_item"],active["work_item_id"])
        self.assertNotIn(question["message_id"],self.books["product"].state()["queued"])
        self.assertEqual(len(state["outbox"]),2)


if __name__=="__main__":unittest.main()
