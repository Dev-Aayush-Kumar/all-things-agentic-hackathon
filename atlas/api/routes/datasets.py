"""Dataset upload routes."""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from atlas.api.dependencies import get_dataset_service
from atlas.domain.exceptions import DatasetNotFoundError, DatasetValidationError
from atlas.domain.models import DatasetUploadResponse
from atlas.services.dataset_service import DatasetService

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post(
    "",
    response_model=DatasetUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_dataset(
    file: UploadFile = File(...),
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetUploadResponse:
    """Upload a CSV dataset for later investigation."""
    content = await file.read()
    try:
        return await service.upload(file.filename, file.content_type, content)
    except DatasetValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get("/{dataset_id}", response_model=DatasetUploadResponse)
async def get_dataset(
    dataset_id: str,
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetUploadResponse:
    """Retrieve uploaded dataset metadata."""
    try:
        return await service.get(dataset_id)
    except DatasetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
