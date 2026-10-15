FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY pyproject.toml README.md LICENSE /app/
COPY app /app/app
RUN pip install --no-cache-dir .
COPY assets /app/assets
COPY prompts /app/prompts
RUN mkdir -p /app/data /app/inbox/json /app/inbox/csv && chown -R app:app /app/data /app/inbox
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
