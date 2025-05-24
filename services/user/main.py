from fastapi import FastAPI, Request 
import time, random
from prometheus_client import make_asgi_app

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.requests import RequestsInstrumentor

# 1. Tracing setup
trace.set_tracer_provider(
    TracerProvider(
        resource=Resource.create({SERVICE_NAME: "user-service"})  # Change per service
    )
)
jaeger_exporter = JaegerExporter(agent_host_name="jaeger", agent_port=6831)
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(jaeger_exporter))

# 2. Metrics setup
metrics.set_meter_provider(
    MeterProvider(metric_readers=[PrometheusMetricReader()])
)
meter = metrics.get_meter(__name__)
hist = meter.create_histogram("http.server.duration", unit="ms")
err_ctr = meter.create_counter("http.server.errors")

# 3. FastAPI App
app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()

@app.get("/users/{user_id}")
async def get_user(user_id: str, request: Request):
    start = time.time()

    # Simulate random failure
    if random.random() < 0.05:
        err_ctr.add(1, {"route": "/users/{user_id}"})
        raise Exception("Simulated error")

    # Simulate latency
    time.sleep(0.05)

    duration = (time.time() - start) * 1000
    hist.record(duration, {"route": "/users/{user_id}"})
    return {"user_id": user_id, "name": "Alice"}

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)