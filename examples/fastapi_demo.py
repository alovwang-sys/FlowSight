"""Small FastAPI integration example for the Phase 0 SDK skeleton."""

from fastapi import FastAPI

from flowsight import FlowSight

app = FastAPI(title="FlowSight demo")
flow_sight = FlowSight(project_root=".", ui_port=4040)
flow_sight.init_app(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
