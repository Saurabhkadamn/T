import asyncio
import uuid
import json
import os
from copy import deepcopy
from datetime import datetime

from graphs.planning.graph import planning_graph
from graphs.execution.graph import build_execution_graph_no_checkpointer


async def main():

    job_id = str(uuid.uuid4())

    execution_trace = []
    final_state_snapshot = None
    planning_result = None

    try:
        # ============================================================
        # 1️⃣ PLANNING PHASE
        # ============================================================

        print("\n===== PLANNING =====\n")

        planning_state = {
            "topic_id": job_id,
            "tenant_id": "test_tenant",
            "user_id": "test_user",
            "chat_bot_id": "bot_1",

            "chat_history": [],
            "uploaded_files": [],
            "file_contents": {},

            "original_topic": (
                "Impact of generative AI tools like GitHub Copilot "
                "on enterprise software development productivity since 2022"
            ),
            "refined_topic": "",

            "needs_clarification": False,
            "clarification_questions": [],
            "clarification_answers": [],
            "clarification_count": 0,

            "depth_of_research": "surface",
            "audience": "",
            "objective": "",
            "domain": "",
            "recency_scope": "",
            "source_scope": [],
            "assumptions": [],

            "plan": None,
            "plan_approved": False,
            "plan_revision_count": 0,
            "checklist": [],
            "self_review_count": 0,

            "status": "pending",
            "error": None,
        }

        planning_result = await planning_graph.ainvoke(
            planning_state,
            config={"configurable": {"thread_id": job_id}},
        )

        plan = planning_result.get("plan")
        checklist = planning_result.get("checklist", [])

        if not plan:
            print("❌ Planning failed.")
            print(planning_result)
            return

        print("✅ Plan sections:", len(plan["sections"]))


        # ============================================================
        # 2️⃣ EXECUTION PHASE (RAW TRACE MODE)
        # ============================================================

        print("\n===== EXECUTION =====\n")

        execution_graph = build_execution_graph_no_checkpointer()

        execution_state = {
            "job_id": job_id,
            "report_id": str(uuid.uuid4()),
            "tenant_id": "test_tenant",
            "user_id": "test_user",

            "topic": planning_result["original_topic"],
            "refined_topic": planning_result["refined_topic"],

            "sections": plan["sections"],
            "plan": plan,
            "checklist": checklist,

            "max_search_iterations": 1,
            "max_sources_per_section": 3,

            "file_contents": {},

            "tools_enabled": {
                "web": True,
                "arxiv": True,
                "content_lake": False,
                "files": False,
            },

            "section_results": [],
            "compressed_findings": [],
            "citations": [],
            "source_scores": [],
            "knowledge_gaps": [],
            "reflection_count": 0,

            "fused_knowledge": "",
            "report_html": "",
            "review_feedback": None,
            "revision_count": 0,

            "status": "initializing",
            "error": None,
            "otel_trace_id": "",
        }

        async for event in execution_graph.astream(
            execution_state,
            config={"configurable": {"thread_id": job_id}},
            stream_mode="debug",
        ):

            timestamp = datetime.utcnow().isoformat()

            # Node Start
            if event.get("type") == "task":
                node_name = event["payload"]["name"]
                print("🔹 START:", node_name)

                execution_trace.append({
                    "timestamp": timestamp,
                    "event_type": "node_start",
                    "node": node_name,
                    "input": deepcopy(event["payload"].get("input", {})),
                })

            # Node Complete
            if event.get("type") == "task_result":
                node_name = event["payload"]["name"]
                print("✅ DONE :", node_name)

                execution_trace.append({
                    "timestamp": timestamp,
                    "event_type": "node_complete",
                    "node": node_name,
                    "output": deepcopy(event["payload"].get("output", {})),
                })

            # State Update
            if event.get("type") == "state":
                final_state_snapshot = deepcopy(event["payload"])

        print("\n===== EXECUTION COMPLETE =====\n")

    except Exception as e:
        print("\n⚠️ Execution crashed:", str(e))

    # ============================================================
    # SAVE TRACE TO SINGLE FILE
    # ============================================================

    trace_output = {
        "job_id": job_id,
        "planning_result": planning_result,
        "execution_trace": execution_trace,
        "final_state": final_state_snapshot,
    }

    with open("trace.json", "w", encoding="utf-8") as f:
        json.dump(trace_output, f, indent=2, ensure_ascii=False)

    print("🧠 Trace written to trace.json")
    print("Absolute path:", os.path.abspath("trace.json"))
    print("File exists:", os.path.exists("trace.json"))


if __name__ == "__main__":
    asyncio.run(main())