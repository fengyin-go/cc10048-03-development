FROM python:3.12-slim
WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV EFFECTIVE_BATCH_STATE_PATH=/srv/app/.data/effective_batch_state.json
RUN mkdir -p /srv/app/.data
EXPOSE 8012
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8012"]
