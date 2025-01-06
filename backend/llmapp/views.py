import os
import json
import numpy as np
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from .models import UploadedPDF, PDFEmbedding, ChatSession, ChatMessage
from .utils import (
    pdf_to_markdown_with_markitdown,
    create_embedding,
    save_faiss_index,
    load_faiss_index,
    create_openai_completion,
    build_prompt_for_pdf,
    extract_text_with_page_numbers,
    createPDFChunk
)
from dotenv import load_dotenv

load_dotenv()
deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


class PDFUploadAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        CHUNK_SIZE=100
        CHUNK_OVERLAP=10
        file = request.FILES.get("file")
        if not file:
            return Response({"error": "No file provided"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            uploaded_pdf = UploadedPDF.objects.create(user=request.user, file=file)

            pdf_path = uploaded_pdf.file.path
            nodes = createPDFChunk(pdf_path, CHUNK_SIZE, CHUNK_OVERLAP)
            print("="*50)
            print("nodes: ", nodes)
            print("="*50)
        except Exception as e:
            return Response({"error": f"Failed to process PDF: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        try:
            embeddings = []
            metadata = []

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(create_embedding, node["text"]): node for node in nodes}
                for future in as_completed(futures):
                    node = futures[future]
                    try:
                        embedding = future.result()
                        embeddings.append(embedding)
                        metadata.append({
                            "page_number": node["page_label"],
                            "text": node["text"]
                        })
                    except Exception as e:
                        print(f"Error processing node: {e}")

            if not embeddings:
                raise RuntimeError("No embeddings were generated.")

            index_file = f"faiss_indices/{uploaded_pdf.id}_index.bin"
            metadata_file = f"faiss_indices/{uploaded_pdf.id}_metadata.json"
            save_faiss_index(embeddings, metadata, index_file, metadata_file)
            uploaded_pdf.processed = True
            uploaded_pdf.embedding_created = True
            uploaded_pdf.processing_progress = 100
            uploaded_pdf.save()
        except Exception as e:
            return Response({"error": f"Failed to save embeddings or metadata: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"message": "PDF processed successfully", "pdf_id": uploaded_pdf.id}, status=status.HTTP_200_OK)


class EvidenceRetrievalAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        pdf_id = request.data.get("pdf_id")
        question = request.data.get("question")
        top_k = int(request.data.get("top_k", 5))

        if not pdf_id or not question:
            return Response({"error": "PDF ID and question are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            index_file = f"faiss_indices/{pdf_id}_index.bin"
            metadata_file = f"faiss_indices/{pdf_id}_metadata.json"
            index, metadata = load_faiss_index(index_file, metadata_file)
            query_embedding = create_embedding(question).reshape(1, -1)

            distances, indices = index.search(query_embedding, top_k=top_k)

            evidence = [metadata[i] for i in indices[0]]

            context = "\n".join([f"Page {item['page_number']}: {item['text']}" for item in evidence])
            print('-'*50)
            prompt = build_prompt_for_pdf(context, question, evidence)
            print("Prompt: ", prompt)


            response = create_openai_completion(prompt)
            print("Response from OpenAI: ", response)
            return Response({
                "question": question,
                "answer": response,
                "evidence": evidence
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to process question: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PDFEmbeddingStatusAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pdf_id, *args, **kwargs):
        pdf = get_object_or_404(UploadedPDF, id=pdf_id, user=request.user)
        return Response({
            "processed": pdf.processed,
            "embedding_created": pdf.embedding_created
        }, status=status.HTTP_200_OK)


class PDFProgressAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pdf_id, *args, **kwargs):
        pdf = get_object_or_404(UploadedPDF, id=pdf_id, user=request.user)
        progress = getattr(pdf, "processing_progress", None)

        if progress is None:
            return Response({"error": "Progress tracking not available for this PDF."}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"progress": progress}, status=status.HTTP_200_OK)


class ChatResponseAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        session_id = request.data.get("session_id")
        message = request.data.get("message")

        if not session_id or not message:
            return Response(
                {"error": "Session ID and message are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        chat_session = get_object_or_404(ChatSession, id=session_id, user=request.user)
        pdf = chat_session.pdf

        try:
            # FAISS 인덱스 및 메타데이터 로드
            index_file = f"faiss_indices/{pdf.id}_index.bin"
            metadata_file = f"faiss_indices/{pdf.id}_metadata.json"
            index, metadata = load_faiss_index(index_file, metadata_file)

            # 질문 임베딩 생성
            query_embedding = create_embedding(message).reshape(1, -1)
            #print(f"Query embedding shape: {query_embedding.shape}, dtype: {query_embedding.dtype}")
            #print(f"Index dimensions: {index.d}")
            #print(f"FAISS index total vectors: {index.ntotal}")

            top_k = min(5, index.ntotal)
            distances, indices = index.search(query_embedding, top_k)

            if not indices[0].size:
                return Response({"error": "No relevant evidence found in the PDF."}, status=status.HTTP_404_NOT_FOUND)

            # 근거추출
            #evidence = [metadata[i] for i in indices[0]]
            evidence = []
            for idx in indices[0]:
                if idx < len(metadata):
                    evidence.append(metadata[idx])
                else:
                    print(f"Warning: Index {idx} out of bounds for metadata.")
            if not evidence:
                evidence.append("No evidence found in this PDF file.")

            print('='*50)
            print("Evidence: ", evidence)
            print('='*50)
            context = "\n".join([f"Page {item['page_number']}: {item['text']}" for item in evidence])
            prompt = build_prompt_for_pdf(context, message, evidence)
            #print("체크포인트")

            response = create_openai_completion(prompt)
            print("response:", response)

            ChatMessage.objects.create(
                session=chat_session,
                sender="user",
                message=message
            )
            
            ChatMessage.objects.create(
                session=chat_session,
                sender="system",
                message=response
            )
            return Response({
                "response": response,
                "evidence": evidence
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to process chat: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StartChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        pdf_id = request.data.get("pdf_id")
        if not pdf_id:
            return Response({"error": "PDF ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        pdf = UploadedPDF.objects.filter(id=pdf_id, user=request.user).first()
        if not pdf or not pdf.processed or not pdf.embedding_created:
            return Response({"error": "PDF is not ready for chat."}, status=status.HTTP_400_BAD_REQUEST)

        chat_session = ChatSession.objects.create(user=request.user, pdf=pdf)
        return Response({"chat_session_id": chat_session.id}, status=status.HTTP_200_OK)