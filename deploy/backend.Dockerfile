FROM mcr.microsoft.com/dotnet/sdk:10.0 AS rag-build
WORKDIR /src
COPY rag-engine/Rag.Search/Rag.Search.csproj ./rag-engine/Rag.Search/
RUN dotnet restore rag-engine/Rag.Search/Rag.Search.csproj
COPY rag-engine/Rag.Search ./rag-engine/Rag.Search
RUN dotnet publish rag-engine/Rag.Search/Rag.Search.csproj -c Release --no-restore -o /out /p:UseAppHost=false

FROM mcr.microsoft.com/dotnet/runtime:10.0 AS rag-runtime

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib \
    OPENBLAS_NUM_THREADS=2 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    DOTNET_ROOT=/usr/share/dotnet \
    PATH="/usr/share/dotnet:${PATH}" \
    RAG_ENGINE_PATH=/app/rag-engine/Rag.Search.dll

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 libicu72 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
# NumPy >=2.4 wheels require x86-64-v2; support the server's older virtual CPU.
RUN pip install --no-cache-dir -r backend/requirements.txt "numpy<2.4"

# RapidOCR stores its default downloaded models in site-packages. Fetch them
# during the build so the non-root runtime never needs to write there or fetch them.
RUN python -c "from rapidocr import RapidOCR; RapidOCR(params={'EngineConfig.onnxruntime.intra_op_num_threads': 2, 'EngineConfig.onnxruntime.inter_op_num_threads': 1, 'Global.log_level': 'error'})"

COPY --from=rag-runtime /usr/share/dotnet /usr/share/dotnet
COPY --from=rag-build /out ./rag-engine

COPY backend ./backend

USER 1000:1000
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
