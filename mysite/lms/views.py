from django.contrib.staticfiles.templatetags.staticfiles import static
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.utils.decorators import method_decorator
from django.views.generic.base import View
from django.db import models

from ct.models import Course
from fsm.models import FSMState
from chat.models import EnrollUnitCode, Chat, Message


class CourseView(View):

    @method_decorator(login_required)
    def get(self, request, course_id):
        """Show list of courselets in a course with proper context"""
        course = get_object_or_404(Course, pk=course_id)
        liveSession = FSMState.find_live_sessions(request.user).filter(
            activity__course=course
        ).first()
        if liveSession:
            liveSession.live_instructor_name = (
                liveSession.user.get_full_name() or liveSession.user.username
            )
            try:
                liveSession.live_instructor_icon = (
                    liveSession.user.instructor.icon_url or static('img/avatar-teacher.jpg')
                )
            except AttributeError:
                liveSession.live_instructor_icon = static('img/avatar-teacher.jpg')
        courselets = (
            (
                courselet,
                EnrollUnitCode.get_code(courselet),
                len(courselet.unit.get_exercises())
            )
            for courselet in course.get_course_units(True)
        )
        live_sessions_history = Chat.objects.filter(
            user=request.user,
            is_live=True,
            enroll_code__courseUnit__course=course,
            state__isnull=True
        )
        # TODO: once django updated to version >=1.8 change next lines to
        # TODO: .annotate(lessons_count=models.Case()).
        # TODO: http://stackoverflow.com/questions/30752268/how-to-filter-objects-for-count-annotation-in-django

        for chat in live_sessions_history:
            chat.lessons_done = Message.objects.filter(
                chat=chat,
                contenttype='unitlesson',
                kind='orct',
                type='message',
                owner=request.user,
            ).count()

        return render(
            request, 'lms/course_page.html',
            dict(
                course=course,
                liveSession=liveSession,
                courslets=courselets,
                livesessions=live_sessions_history,
            )
        )
