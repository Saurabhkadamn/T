import asyncio
import uuid

from graphs.planning.graph import planning_graph
from graphs.planning.state import PlanningState


async def main():

    topic_id = str(uuid.uuid4())

    initial_state: PlanningState = {
        # Identity
        "topic_id": topic_id,
        "tenant_id": "test_tenant",
        "user_id": "test_user",
        "chat_bot_id": "bot_1",

        # Input context
        "chat_history": [],
        "uploaded_files": [],
        "file_contents": {},

        # Topic
        "original_topic": "AI",
        "refined_topic": "",

        # Clarification
        "needs_clarification": True,
        "clarification_questions": [],
        "clarification_answers": [],
        "clarification_count": 0,

        # Research parameters
        "depth_of_research": "intermediate",
        "audience": "",
        "objective": "",
        "domain": "",
        "recency_scope": "",
        "source_scope": [],
        "assumptions": [],

        # Plan
        "plan": None,
        "plan_approved": False,
        "plan_revision_count": 0,
        "checklist": [],
        "self_review_count": 0,

        # Lifecycle
        "status": "pending",
        "error": None,
    }

    print("\n=== EXECUTION TRACE ===")

    async for step in planning_graph.astream(
        initial_state,
        config={"configurable": {"thread_id": topic_id}},
    ):
        print(step)

    print("\n=== DONE ===")


asyncio.run(main())