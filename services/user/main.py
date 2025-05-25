from fastapi import FastAPI, Request, HTTPException
import time, random, os
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.requests import RequestsInstrumentor

from sqlalchemy import create_engine, Column, String
from sqlalchemy.orm import declarative_base, Session

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/users_db")
engine = create_engine(DATABASE_URL)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)
    name = Column(String)
    email = Column(String)
    dob = Column(String)
    address = Column(String)

Base.metadata.create_all(engine)

# Pydantic model for input validation
class UserIn(BaseModel):
    id: str
    name: str
    email: str
    dob: str
    address: str

# Tracing setup
trace.set_tracer_provider(
    TracerProvider(resource=Resource.create({SERVICE_NAME: "user-service"}))
)
jaeger_exporter = JaegerExporter(agent_host_name="jaeger", agent_port=6831)
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(jaeger_exporter))
tracer = trace.get_tracer(__name__)

# Metrics setup
metrics.set_meter_provider(MeterProvider(metric_readers=[PrometheusMetricReader()]))
meter = metrics.get_meter(__name__)
hist = meter.create_histogram("http.server.duration", unit="ms")
err_ctr = meter.create_counter("http.server.errors")

# FastAPI App
app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()

@app.post("/users")
async def create_user(user: UserIn):
    with tracer.start_as_current_span("create_user") as span:
        span.set_attribute("user.id", user.id)
        span.set_attribute("user.email", user.email)
        with Session(engine) as session:
            existing = session.get(User, user.id)
            if existing:
                span.set_attribute("user.exists", True)
                raise HTTPException(status_code=400, detail="User already exists")
            new_user = User(**user.dict())
            session.add(new_user)
            session.commit()
            return {"status": "created"}

@app.get("/users/{user_id}")
async def get_user(user_id: str, request: Request):
    start = time.time()
    with tracer.start_as_current_span("get_user_lookup") as span:
        span.set_attribute("user.id", user_id)
        with Session(engine) as session:
            user = session.get(User, user_id)
            if not user:
                span.set_attribute("user.found", False)
                err_ctr.add(1, {"route": "/users/{user_id}"})
                raise HTTPException(status_code=404, detail="User not found")

            duration = (time.time() - start) * 1000
            hist.record(duration, {"route": "/users/{user_id}"})

            span.set_attribute("user.found", True)
            return {
                "user_id": user.id,
                "name": user.name,
                "email": user.email,
                "dob": user.dob,
                "address": user.address
            }

# Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)