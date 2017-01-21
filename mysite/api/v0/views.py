from rest_framework import viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAdminUser

from ..serializers import ResponseSerializer, ErrorSerializer
from ct.models import Response, StudentError


class ResponseViewSet(viewsets.mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Django RestFramework class to implement Course student responses report.
    """
    authentication_classes = (SessionAuthentication,)
    permission_classes = (IsAdminUser,)
    queryset = Response.objects.filter(kind='orct', unitLesson__order__isnull=False)
    serializer_class = ResponseSerializer

    def get_queryset(self):
        queryset = super(ResponseViewSet, self).get_queryset()
        return queryset.filter(course__id=self.kwargs.get('course_id'))


class ErrorViewSet(viewsets.mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Django RestFramework class to implement Course student errors report.
    """
    authentication_classes = (SessionAuthentication,)
    permission_classes = (IsAdminUser,)
    queryset = StudentError.objects.all()
    serializer_class = ErrorSerializer

    def get_queryset(self):
        queryset = super(ErrorViewSet, self).get_queryset()
        return queryset.filter(response__course__id=self.kwargs.get('course_id'))