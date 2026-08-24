FROM python:3.12-slim

WORKDIR /srv

# torch на CPU: обычное колесо тянет ~2.5 ГБ ради CUDA, которая на сервере не нужна
ENV PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# веса кладём в том, иначе они качаются заново при каждом пересоздании контейнера
ENV HF_HOME=/models-cache
ENV OMP_NUM_THREADS=4

EXPOSE 8081
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8081"]
