from fastapi import FastAPI

app = FastAPI(title="gateway")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gateway"}
