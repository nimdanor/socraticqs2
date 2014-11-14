from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils.safestring import mark_safe
from django.utils import timezone
from django.http import HttpResponseRedirect, HttpResponse
from django.core.urlresolvers import reverse
from django.core.exceptions import ObjectDoesNotExist
from django.views.decorators.csrf import ensure_csrf_cookie
from django.db.models import Q
import json
from ct.models import *
from ct.forms import *
from ct.templatetags.ct_extras import md2html
from ct.fsm import FSMStack

######################################################
# student live session UI

def live_response(request, r=None):
    'if in live session, synchronize students to desired stage'
    fsmStack = FSMStack(request)
    try:
        liveSession = fsmStack.state.liveSession
    except AttributeError:
        return None, None
    if liveSession.endTime: # all done
        rm_live_user(request, liveSession)
        fsmStack.pop(request)
        return None, None
    stage, r = liveSession.get_next_stage(request.user, r)
    if stage == liveSession.WAIT: # just wait
        return render(request, 'ct/wait.html',
                      dict(actionTarget=reverse('ct:live'))), liveSession
    liveQuestion = liveSession.liveQuestion
    url = liveQuestion.next_url(stage, r)
    if r and r.liveQuestion != liveQuestion:
        return render(request, 'ct/wait.html',
                    dict(actionTarget=url,
                    message='''Please click Next to join the new question
                    the instructor has assigned.  You can return to follow
                    up on this exercise after class (online).''')), liveSession
    return HttpResponseRedirect(url), liveSession


def start_live_user(request, courseQuestion):
    try:
        liveSession = LiveSession.get_from_request(request)
    except KeyError:
        return
    if liveSession.liveQuestion and \
      liveSession.liveQuestion.courseQuestion == courseQuestion:
        liveSession.liveQuestion.start_user_session(request.user)

@login_required
def respond_cq(request, cq_id):
    courseQuestion = get_object_or_404(CourseQuestion, pk=cq_id)
    return _respond(request, courseQuestion.question, courseQuestion)


@login_required
def respond(request, ct_id):
    return _respond(request, get_object_or_404(Question, pk=ct_id))

def _respond(request, q, courseQuestion=None):
    'ask student a question'
    start_live_user(request, courseQuestion) # add live user if appropriate
    if request.method == 'POST':
        form = ResponseForm(request.POST)
        if form.is_valid():
            r = form.save(commit=False)
            r.question = q
            r.courseQuestion = courseQuestion
            r.atime = timezone.now()
            r.author = request.user
            response, liveSession = live_response(request, r)
            if liveSession:
                r.liveQuestion = liveSession.get_live_question(courseQuestion)
            r.save()
            if response: # let LIVE mode override default next step
                return response
            return HttpResponseRedirect(reverse('ct:assess',
                                                args=(r.id,)))
    else:
        form = ResponseForm()
    set_crispy_action(request.path, form)
    return render(request, 'ct/ask.html',
                  dict(question=q, qtext=md2html(q.qtext), form=form,
                       actionTarget=request.path))

@login_required
def assess(request, resp_id):
    'student self-assessment'
    r = get_object_or_404(Response, pk=resp_id)
    errors = r.courseQuestion.courseerrormodel_set.all()
    choices = [(e.id, md2html(e.errorModel.description, stripP=True))
               for e in errors]
    if request.method == 'POST':
        form = SelfAssessForm(request.POST)
        form.fields['emlist'].choices = choices
        if form.is_valid():
            r.selfeval = form.cleaned_data['selfeval']
            r.status = form.cleaned_data['status']
            r.save()
            for emID in form.cleaned_data['emlist']:
                em = get_object_or_404(CourseErrorModel, pk=emID)
                se = r.studenterror_set.create(courseErrorModel=em,
                                               author=r.author)
            response, liveSession = live_response(request, r)
            if response: # let LIVE mode override default next step
                return response
            return HttpResponseRedirect('/ct/')
    else:
        form = SelfAssessForm()
        form.fields['emlist'].choices = choices 

    return render(request, 'ct/assess.html',
                  dict(response=r, qtext=md2html(r.question.qtext),
                       answer=md2html(r.question.answer), form=form,
                       actionTarget=request.path))


@login_required
def live_session(request):
    'keep student waiting until instructor advances live exercise'
    response, liveSession = live_response(request)
    if response:
        return response
    return HttpResponseRedirect(reverse('ct:home'))

    
#############################################################
# instructor CourseQuestion live session UI
    
def check_instructor_auth(course, request):
    role = course.get_user_role(request.user)
    if role != Role.INSTRUCTOR:
        return HttpResponse("Only the instructor can access this",
                            status_code=403)

def check_liveinst_auth(request):
    fsmStack = FSMStack(request)
    liveSession = fsmStack.state.liveSession
    liveQuestion = fsmStack.state.liveQuestion
    courseQuestion = liveQuestion.courseQuestion
    return (check_instructor_auth(liveSession.course, request),
            liveQuestion, courseQuestion, fsmStack)
    
@login_required
def live_start(request):
    'instructor live session START page'
    notInstructor, liveQuestion, courseQuestion, fsmStack = check_liveinst_auth(request)
    if notInstructor:
        return notInstructor
    if request.method == 'POST':
        if request.POST.get('task') == 'live_control':
            liveQuestion.startTime = timezone.now()
            liveQuestion.save() # save time stamp
            return fsmStack.transition(request, 'live_control')
    return render(request, 'ct/livestart.html',
                  dict(courseQuestion=courseQuestion,
                       actionTarget=request.path,
                       qtext=md2html(courseQuestion.question.qtext),
                       answer=md2html(courseQuestion.question.answer)))

@login_required
def live_control(request):
    'instructor live session UI for monitoring student responses'
    notInstructor, liveQuestion, courseQuestion, fsmStack = check_liveinst_auth(request)
    if notInstructor: # must be instructor to use this interface
        return notInstructor
    responses = liveQuestion.response_set.all() # responses from live session
    sure = responses.filter(confidence=Response.SURE)
    unsure = responses.filter(confidence=Response.UNSURE)
    guess = responses.filter(confidence=Response.GUESS)
    nuser = liveQuestion.liveuser_set.count() # count logged in users
    counts = [guess.count(), unsure.count(), sure.count(), 0]
    counts[-1] = nuser - sum(counts)
    sec = (timezone.now() - liveQuestion.startTime).seconds
    elapsedTime = '%d:%02d' % (sec / 60, sec % 60)
    ndisplay = 25 # set default values
    sortOrder = '-atime'
    rlform = ResponseListForm()
    if request.method == 'POST': # create a new ErrorModel
        if request.POST.get('task') == 'live_end':
            liveQuestion.liveStage = liveQuestion.ASSESSMENT_STAGE
            liveQuestion.save()
            return fsmStack.transition(request, 'live_end')
        else:
            emform = ErrorModelForm(request.POST)
            if emform.is_valid():
                e = emform.save(commit=False)
                e.author = request.user
                e.save()
                courseQuestion.courseerrormodel_set.create(errorModel=e,
                    course=courseQuestion.courselet.course,
                    addedBy=request.user)
    else:
        emform = ErrorModelForm()
        if request.GET: # new query parameters for displaying responses
            rlform = ResponseListForm(request.GET)
            if rlform.is_valid():
                ndisplay = int(rlform.cleaned_data['ndisplay'])
                sortOrder = rlform.cleaned_data['sortOrder']
    responses.order_by(sortOrder) # apply the desired sort order
    set_crispy_action(request.path, emform)
    return render(request, 'ct/control.html',
                  dict(courseQuestion=courseQuestion,
                       qtext=md2html(courseQuestion.question.qtext),
                       answer=md2html(courseQuestion.question.answer),
                       counts=counts, elapsedTime=elapsedTime, 
                       actionTarget=request.path,
                       emform=emform, responses=responses[:ndisplay],
                       rlform=rlform))

def count_vectors(data, start=None, end=None):
    d = {}
    for t in data:
        k = t[start:end]
        d[k] = d.get(k, 0) + 1
    return d

def make_table(d, keyset, func, t=()):
    keys = keyset[0]
    keyset = keyset[1:]
    l = []
    for k in keys:
        kt = t + (k,)
        if keyset:
            l.append(make_table(d, keyset, func, kt))
        else:
            l.append(func(d.get(kt, 0)))
    return l

@login_required
def live_end(request):
    'instructor live session UI for monitoring student self-assessment'
    notInstructor, liveQuestion, courseQuestion, fsmStack = check_liveinst_auth(request)
    if notInstructor: # must be instructor to use this interface
        return notInstructor
    if request.method == 'POST':
        if request.POST.get('task') == 'finish':
            liveQuestion.end()
            return fsmStack.transition(request, 'live_session',
                        dict(courselet_id=courseQuestion.courselet.id))
    n = liveQuestion.response_set.count() # count all responses from live session
    responses = liveQuestion.response_set.exclude(selfeval=None) # self-assessed
    statusCounts, evalCounts, ndata = status_confeval_tables(responses, n)
    errorCounts = liveQuestion.errormodel_table(ndata)
    sec = (timezone.now() - liveQuestion.startTime).seconds
    elapsedTime = '%d:%02d' % (sec / 60, sec % 60)
    return render(request, 'ct/end.html',
                  dict(courseQuestion=courseQuestion,
                       qtext=md2html(courseQuestion.question.qtext),
                       answer=md2html(courseQuestion.question.answer),
                       statusCounts=statusCounts, elapsedTime=elapsedTime,
                       evalCounts=evalCounts, actionTarget=request.path,
                       refreshRate=15, errorCounts=errorCounts))


def status_confeval_tables(responses, n):
    data = [(r.confidence, r.selfeval, r.status) for r in responses]
    ndata = len(data)
    if ndata == 0:
        return (), (), 0
    statusCounts = count_vectors(data, -1)
    evalCounts = count_vectors(data, end=2)
    fmt_count = lambda c: '%d (%.0f%%)' % (c, c * 100. / n)
    confKeys = [t[0] for t in Response.CONF_CHOICES]
    confLabels = [t[1] for t in Response.CONF_CHOICES]
    evalKeys = [t[0] for t in Response.EVAL_CHOICES]
    statusKeys = [t[0] for t in Response.STATUS_CHOICES]
    statusCounts = make_table(statusCounts, (statusKeys,), fmt_count)
    statusCounts.append(fmt_count(n - ndata))
    if ndata > 0: # build the self-assessment table
        fmt_count = lambda c: '%d (%.0f%%)' % (c, c * 100. / ndata)
        evalCounts = make_table(evalCounts, (confKeys, evalKeys), fmt_count)
        evalCounts = zip(confLabels, evalCounts)
    else: # no data so don't display anything
        evalCounts = ()
    return statusCounts, evalCounts, ndata

######################################################3
# UI for searching, creating Question entries

def new_question(request):
    'create new Question from POST form data'
    form = NewQuestionForm(request.POST)
    if form.is_valid():
        question = form.save(commit=False)
        question.author = request.user
        question.save()
        return question, form
    return None, form

def new_lesson(request):
    'create new Lesson from POST form data'
    form = NewLessonForm(request.POST)
    if form.is_valid():
        lesson = form.save(commit=False)
        lesson.addedBy = request.user
        lesson.rustID = ''
        lesson.save()
        return lesson, form
    return None, form

@login_required
def questions(request):
    'search or create a Question'
    qset = ()
    if request.method == 'POST':
        question, qform = new_question(request)
        if question:
            return HttpResponseRedirect(reverse('ct:question',
                                                args=(question.id,)))
    elif 'search' in request.GET:
        searchForm = QuestionSearchForm(request.GET)
        if searchForm.is_valid():
            s = searchForm.cleaned_data['search']
            qset = Question.objects.filter(Q(title__icontains=s) |
                                           Q(qtext__icontains=s) |
                                           Q(answer__icontains=s))
    else:
        searchForm = QuestionSearchForm()
    return render(request, 'ct/questions.html',
                  dict(qset=qset, actionTarget=request.path,
                       searchForm=searchForm))

@ensure_csrf_cookie
def question(request, ct_id):
    'generic page for a specific question'
    q = get_object_or_404(Question, pk=ct_id)
    qform = QuestionForm(instance=q)
    if request.method == 'POST':
        qform = QuestionForm(request.POST, instance=q)
        if qform.is_valid():
            qform.save()    
    try:
        sl = StudyList.objects.get(question=q, user=request.user)
        inStudylist = 1
    except ObjectDoesNotExist:
        inStudylist = 0
    responses = q.response_set.exclude(selfeval=None) # self-assessed
    n = len(responses)
    statusCounts, evalCounts, ndata = status_confeval_tables(responses, n)
    errorCounts = q.errormodel_table(ndata)
    set_crispy_action(request.path, qform)
    return render(request, 'ct/question.html',
                  dict(question=q, qtext=md2html(q.qtext),
                       answer=md2html(q.answer),
                       qform=qform,
                       actionTarget=request.path,
                       inStudylist=inStudylist,
                       allowEdit=(q.author == request.user),
                       atime=display_datetime(q.atime),
                       statusCounts=statusCounts, evalCounts=evalCounts,
                       errorCounts=errorCounts))


@login_required
def flag_question(request, ct_id):
    'JSON POST for adding / removing question from study list'
    q = get_object_or_404(Question, pk=ct_id)
    if request.is_ajax() and request.method == 'POST':
        newstate = int(request.POST['state'])
        try:
            sl = StudyList.objects.get(question=q, user=request.user)
            if not newstate:
                sl.delete()
        except ObjectDoesNotExist:
            if newstate:
                q.studylist_set.create(user=request.user)
        data = json.dumps(dict(newstate=newstate))
        return HttpResponse(data, content_type='application/json')


@login_required
def question_concept(request, ct_id):
    q = get_object_or_404(Question, pk=ct_id)
    if q.author != request.user:
        return HttpResponse("Only the author can edit this",
                            status_code=403)
    r = _concepts(request, '''Please choose a Concept that best describes
    what this question aims to test, by entering a search term to
    find relevant concepts.''')
    if isinstance(r, Concept): # user chose a concept to link
        q.concept = r # link question to this concept
        q.save()
        return HttpResponseRedirect(reverse('ct:question', args=(q.id,)))
    return r

@login_required
def error_model(request, em_id):
    em = get_object_or_404(ErrorModel, pk=em_id)
    if em.author != request.user:
        return HttpResponse("Only the author can edit this",
                            status_code=403)
    relatedErrors = () # need to fix this!!!
    if em.alwaysAsk:
        n = Response.objects.count()
    else:
        n = Response.objects.filter(selfeval__isnull=False,
                courseQuestion__courseerrormodel__errorModel=em).count()
    if n > 0:
        nerr = Response.objects.filter(
            studenterror__courseErrorModel__errorModel=em).count()
        emPercent = '%.0f' % (nerr * 100. / n)
    else:
        emPercent = None
    
    emform = ErrorModelForm(instance=em)
    nrform = NewRemediationForm()
    if request.method == 'POST':
        if 'description' in request.POST:
            emform = ErrorModelForm(request.POST, instance=em)
            if emform.is_valid():
                emform.save()
        elif 'title' in request.POST:
            nrform = NewRemediationForm(request.POST)
            if nrform.is_valid():
                remedy = nrform.save(commit=False)
                remedy.errorModel = em
                remedy.author = request.user
                remedy.save()
                return HttpResponseRedirect(reverse('ct:remediation',
                                                    args=(remedy.id,)))
    set_crispy_action(request.path, nrform) # set actionTarget directly
    return render(request, 'ct/errormodel.html',
                  dict(em=em, actionTarget=request.path, emform=emform,
                       atime=display_datetime(em.atime), nrform=nrform,
                       relatedErrors=relatedErrors,
                       emPercent=emPercent, N=n))

@login_required
def remediation(request, rem_id):
    remedy = get_object_or_404(Remediation, pk=rem_id)
    if remedy.author != request.user:
        return HttpResponse("Only the author can edit this",
                            status_code=403)
    titleform = RemediationForm(instance=remedy)
    searchForm, sourceDB, lessonSet, wset = _search_lessons(request)
    if request.method == 'POST':
        if 'advice' in request.POST:
            titleform = RemediationForm(request.POST, instance=remedy)
            if titleform.is_valid():
                titleform.save()
        else:
            _post_lesson(request, remedy)
    set_crispy_action(request.path, titleform) # set actionTarget directly
    return render(request, 'ct/remediation.html',
                  dict(remedy=remedy, sourceDB=sourceDB,
                       lessonSet=lessonSet, actionTarget=request.path,
                       searchForm=searchForm, wset=wset,
                       titleform=titleform,
                       atime=display_datetime(remedy.atime)))

def _post_lesson(request, remedy):
    'form interface for adding and removing lessons'
    if 'sourceID' in request.POST:
        lesson = Lesson.get_from_sourceDB(request.POST.get('sourceID'),
                    request.user, request.POST.get('sourceDB'))
        remedy.lessons.add(lesson)
    elif request.POST.get('task') == 'rmLesson':
        lesson = Lesson.objects.get(pk=int(request.POST.get('lessonID')))
        remedy.lessons.remove(lesson)
    elif 'lessonID' in request.POST:
        lesson = Lesson.objects.get(pk=int(request.POST.get('lessonID')))
        remedy.lessons.add(lesson)

def _search_lessons(request):
    'form interface for search Lessons and external sourceDB'
    searchForm = LessonSearchForm()
    lessonSet = wset = ()
    sourceDB = ''
    if 'search' in request.GET:
        searchForm = LessonSearchForm(request.GET)
        if searchForm.is_valid():
            s = searchForm.cleaned_data['search']
            sourceDB = searchForm.cleaned_data['sourceDB']
            lessonSet = Lesson.objects.filter(Q(title__icontains=s) |
                                              Q(text__icontains=s))
            wset = Lesson.search_sourceDB(s, sourceDB)
    ## searchForm.helper.form_action = request.path # set actionTarget directly
    return searchForm, sourceDB, lessonSet, wset

def lesson(request, lesson_id):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    if request.user == lesson.addedBy:
        titleform = LessonForm(instance=lesson)
        if request.method == 'POST':
            if 'title' in request.POST: # update courselet attributes
                titleform = LessonForm(request.POST, instance=lesson)
                if titleform.is_valid():
                    titleform.save()
            elif request.POST.get('task') == 'delete': # delete me
                lesson.delete()
                return HttpResponseRedirect(reverse('ct:teach'))

    else:
        titleform = None
    return render(request, 'ct/lesson.html',
                  dict(user=request.user, actionTarget=request.path,
                       lesson=lesson, titleform=titleform))
    
#################################################
# instructor course UI

@login_required
def teach(request):
    'top-level instructor UI'
    courseform = NewCourseTitleForm()
    set_crispy_action(reverse('ct:courses'), courseform)
    courses = Course.objects.filter(role__role=Role.INSTRUCTOR,
                                    role__user=request.user)
    return render(request, 'ct/teach.html',
                  dict(user=request.user, actionTarget=request.path,
                       courses=courses, courseform=courseform))

@login_required
def courses(request):
    'list courses or create new course'
    if request.method == 'POST':
        form = CourseTitleForm(request.POST)
        if form.is_valid():
            course = form.save(commit=False)
            course.addedBy = request.user
            course.save()
            role = Role(course=course, user=request.user,
                        role=Role.INSTRUCTOR)
            role.save()
            return HttpResponseRedirect(reverse('ct:course',
                                                args=(course.id,)))

    courseSet = Course.objects.filter(courselet__coursequestion__isnull=False)
    return render(request, 'ct/courses.html', dict(courses=courseSet))


@login_required
def course(request, course_id):
    'instructor UI for managing a course'
    course = get_object_or_404(Course, pk=course_id)
    notInstructor = check_instructor_auth(course, request)
    if notInstructor: # redirect students to live session or student page
        return redirect_live(request,
          HttpResponseRedirect(reverse('ct:course_study', args=(course.id,))))
    courseletform = NewCourseletTitleForm()
    titleform = CourseTitleForm(instance=course)
    fsmStack = FSMStack(request)
    if request.method == 'POST':
        if 'access' in request.POST: # update course attrs
            titleform = CourseTitleForm(request.POST, instance=course)
            if titleform.is_valid():
                titleform.save()
        elif 'title' in request.POST: # create new courselet
            courseletform = NewCourseletTitleForm(request.POST)
            if courseletform.is_valid():
                courselet = courseletform.save(commit=False)
                courselet.course = course
                courselet.addedBy = request.user
                courselet.save()
                return HttpResponseRedirect(reverse('ct:courselet',
                                                    args=(courselet.id,)))
        elif request.POST.get('task') == 'delete': # delete me
            course.delete()
            return HttpResponseRedirect(reverse('ct:teach'))
        elif request.POST.get('task') == 'livestart': # create live session
            liveSession = LiveSession(course=course, addedBy=request.user)
            liveSession.save()
            fsmStack.push(request, 'liveInstructor',
                          dict(liveSession=liveSession))
        elif request.POST.get('task') == 'liveend': # end live session
            liveSession = fsmStack.state.liveSession
            liveSession.liveQuestion = None
            liveSession.endTime = timezone.now() # mark as completed
            liveSession.save()
            fsmStack.pop(request)
    set_crispy_action(request.path, courseletform, titleform)
    return render(request, 'ct/course.html',
                  dict(course=course, actionTarget=request.path,
                       titleform=titleform, fsmStack=fsmStack,
                       courseletform=courseletform))

def get_slform(courselet, user):
    questions = Question.objects.filter(Q(studylist__user=user) |
                Q(concept__courselet=courselet)) \
                .exclude(coursequestion__courselet=courselet)
    if questions.count() > 0:
        return CourseQuestionForm(questions)

@login_required
def courselet(request, courselet_id):
    'instructor UI for managing a courselet'
    courselet = get_object_or_404(Courselet, pk=courselet_id)
    notInstructor = check_instructor_auth(courselet.course, request)
    if notInstructor: # must be instructor to use this interface
        return notInstructor
    fsmStack = FSMStack(request)
    qform = NewQuestionForm()
    lform = NewLessonForm()
    titleform = CourseletTitleForm(instance=courselet)
    slform = get_slform(courselet, request.user)
    if request.method == 'POST':
        if 'qtext' in request.POST: # create new exercise
            question, qform = new_question(request)
            if question:
                n = courselet.courselesson_set.count() + \
                  courselet.coursequestion_set.count()
                courseQuestion = CourseQuestion(courselet=courselet,
                                                question=question, order=n,
                                                addedBy=request.user)
                courseQuestion.save()
                qform = NewQuestionForm() # new blank form to display
        elif 'kind' in request.POST: # create new lesson
            lesson, lform = new_lesson(request)
            if lesson:
                n = courselet.courselesson_set.count() + \
                  courselet.coursequestion_set.count()
                courseLesson = CourseLesson(courselet=courselet,
                                            lesson=lesson,
                                            order=n, addedBy=request.user)
                courseLesson.save()
                lform = NewLessonForm() # new blank form to display
        elif 'question' in request.POST: # add new CourseQuestion
            slform = CourseQuestionForm(None, request.POST)
            if slform.is_valid():
                courseQuestion = slform.save(commit=False)
                courseQuestion.courselet = courselet
                courseQuestion.addedBy = request.user
                courseQuestion.save()
                if courseQuestion.question.concept is None: # need to choose concept
                    return HttpResponseRedirect(reverse('ct:cq_concept',
                                                args=(courseQuestion.id,)))
                slform = get_slform(courselet, request.user) # fresh form
        elif 'title' in request.POST: # update courselet attributes
            titleform = CourseletTitleForm(request.POST, instance=courselet)
            if titleform.is_valid():
                titleform.save()
        elif request.POST.get('task') == 'delete': # delete me
            course = courselet.course
            courselet.delete()
            return HttpResponseRedirect(reverse('ct:course',
                                                args=(course.id,)))
    set_crispy_action(request.path, qform, lform, titleform)    
    return render(request, 'ct/courselet.html',
                  dict(courselet=courselet, actionTarget=request.path,
                       slform=slform, titleform=titleform, qform=qform,
                       fsmStack=fsmStack,
                       exercises=courselet.get_exercises(), lform=lform))

@login_required
def course_question(request, cq_id):
    'instructor CourseQuestion report / management page'
    courseQuestion = get_object_or_404(CourseQuestion, pk=cq_id)
    notInstructor = check_instructor_auth(courseQuestion.courselet.course,
                                          request)
    if notInstructor:
        return notInstructor
    emform = ErrorModelForm()
    fsmStack = FSMStack(request)
    if request.method == 'POST':
        if 'description' in request.POST:
            emform = ErrorModelForm(request.POST)
            if emform.is_valid():
                e = emform.save(commit=False)
                e.author = request.user
                e.save()
                courseQuestion.courseerrormodel_set.create(errorModel=e,
                    course=courseQuestion.courselet.course,
                    addedBy=request.user)
                emform = ErrorModelForm() # new blank form
        elif request.POST.get('task') == 'livestart':
            liveSession = fsmStack.state.liveSession
            liveQuestion = LiveQuestion(liveSession=liveSession,
                                        courseQuestion=courseQuestion,
                                        addedBy=request.user)
            liveQuestion.start() # calls save()
            fsmStack.state.liveQuestion = liveQuestion
            return fsmStack.transition(request, 'livestart')
        elif request.POST.get('task') == 'delete':
            courselet = courseQuestion.courselet
            courseQuestion.delete()
            return HttpResponseRedirect(reverse('ct:courselet',
                                                args=(courselet.id,)))
    n = courseQuestion.response_set.count() # count all responses
    responses = courseQuestion.response_set.exclude(selfeval=None) # self-assessed
    statusCounts, evalCounts, ndata = status_confeval_tables(responses, n)
    errorCounts = courseQuestion.errormodel_table(ndata, includeAll=True)
    uncats = Response.objects.filter(courseQuestion=courseQuestion,
        studenterror__isnull=True).exclude(selfeval=Response.CORRECT)
    uncats.order_by('status')
    return render(request, 'ct/course_question.html',
                  dict(courseQuestion=courseQuestion,
                       qtext=md2html(courseQuestion.question.qtext),
                       answer=md2html(courseQuestion.question.answer),
                       statusCounts=statusCounts, uncategorized=uncats,
                       evalCounts=evalCounts, actionTarget=request.path,
                       errorCounts=errorCounts, emform=emform))

@login_required
def course_lesson(request, cl_id):
    'instructor CourseLesson report / management page'
    courseLesson = get_object_or_404(CourseLesson, pk=cl_id)
    notInstructor = check_instructor_auth(courseLesson.courselet.course,
                                          request)
    if notInstructor:
        return notInstructor
    if request.method == 'POST':
        if request.POST.get('task') == 'delete':
            courselet = courseLesson.courselet
            courseLesson.delete()
            return HttpResponseRedirect(reverse('ct:courselet',
                                                args=(courselet.id,)))


@login_required
def courselet_concept(request, courselet_id):
    courselet = get_object_or_404(Courselet, pk=courselet_id)
    notInstructor = check_instructor_auth(courselet.course, request)
    if notInstructor: # must be instructor to use this interface
        return notInstructor
    r = _concepts(request, '''Please choose a Concept that this courselet
    will teach, by entering a search term to
    find relevant concepts.''')
    if isinstance(r, Concept): # user chose a concept to link
        if r in tuple(courselet.concepts.all()):
            return _concepts(request, '''You have already added that concept
        to this courselet.''', ignorePOST=True)
        else:
            courselet.concepts.add(r) # link to this concept
            return HttpResponseRedirect(reverse('ct:courselet',
                                            args=(courselet.id,)))
    return r


@login_required
def cq_concept(request, cq_id):
    courseQuestion = get_object_or_404(CourseQuestion, pk=cq_id)
    notInstructor = check_instructor_auth(courseQuestion.courselet.course,
                                          request)
    if notInstructor: # must be instructor to use this interface
        return notInstructor
    r = _concepts(request, '''Please choose a Concept that best describes
    what this question aims to test, by entering a search term to
    find relevant concepts.''')
    if isinstance(r, Concept): # user chose a concept to link
        courseQuestion.question.concept = r # link question to this concept
        courseQuestion.question.save()
        return HttpResponseRedirect(reverse('ct:courselet',
                            args=(courseQuestion.courselet.id,)))
    return r


@login_required
def lesson_concept(request, lesson_id):
    lesson = get_object_or_404(Lesson, pk=lesson_id)
    if request.user != lesson.addedBy:
        return HttpResponse("Only the lesson author can access this",
                            status_code=403)
    r = _concepts(request, '''Please choose a Concept that this lesson
    will teach, by entering a search term to
    find relevant concepts.''')
    if isinstance(r, Concept): # user chose a concept to link
        if r in tuple(Concept.objects.filter(lessonlink__lesson=lesson).all()):
            return _concepts(request, '''You have already added that concept
        to this leson.''', ignorePOST=True)
        else:
            ll = LessonLink(lesson=lesson, concept=r, addedBy=request.user)
            ll.save()
            return HttpResponseRedirect(reverse('ct:lesson',
                                            args=(lesson.id,)))
    return r


@login_required
def concepts(request):
    r = _concepts(request)
    if isinstance(r, Concept):
        return _concepts(request, '''Successfully added concept.
            Thank you!''', ignorePOST=True)
    return r

###########################################################
# WelcomeMat refactored views

def update_concept_link(request, conceptLinks):
    cl = get_object_or_404(ConceptLink, pk=int(request.POST.get('clID')))
    clform = ConceptLinkForm(request.POST, instance=cl)
    if clform.is_valid():
        clform.save()
        conceptLinks.replace(cl, clform)

def _concepts(request, msg='', ignorePOST=False, conceptLinks=None,
              toTable=None, fromTable=None, pageTitle='Concepts', unit=None,
              actionLabel='Link to this Concept', navTabs=(),
              errorModels=None, **kwargs):
    'search or create a Concept'
    cset = wset = ()
    if errorModels is not None:
        conceptForm = NewConceptForm()
    else:
        conceptForm = None
    searchForm = ConceptSearchForm()
    if request.method == 'POST' and not ignorePOST:
        if 'wikipediaID' in request.POST:
            concept, lesson = \
              Concept.get_from_sourceDB(request.POST.get('wikipediaID'),
                                        request.user)
            if unit:
                lesson.unitlesson_set.create(unit=unit, treeID=lesson.treeID,
                                             addedBy=concept.addedBy)
            return concept
        elif 'clID' in request.POST:
            update_concept_link(request, conceptLinks)
        elif request.POST.get('task') == 'reverse' and 'cgID' in request.POST:
            cg = get_object_or_404(ConceptGraph,
                                   pk=int(request.POST.get('cgID')))
            c = cg.fromConcept
            cg.fromConcept = cg.toConcept
            cg.toConcept = c
            cg.save() # save the reversed relationship
            toTable.move_between_tables(cg, fromTable)
        elif 'cgID' in request.POST:
            cg = get_object_or_404(ConceptGraph,
                                   pk=int(request.POST.get('cgID')))
            cgform = ConceptGraphForm(request.POST, instance=cg)
            if cgform.is_valid():
                cgform.save()
                toTable.replace(cg, cgform)
                fromTable.replace(cg, cgform)
        elif 'conceptID' in request.POST:
            return Concept.objects.get(pk=int(request.POST.get('conceptID')))
        elif 'title' in request.POST: # create new concept
            conceptForm = NewConceptForm(request.POST)
            if conceptForm.is_valid():
                concept = conceptForm.save(commit=False)
                concept.addedBy = request.user
                concept.save()
                UnitLesson.create_from_concept(concept, unit)
                return concept
        else:
            return 'please write POST error message'
    elif 'search' in request.GET:
        searchForm = ConceptSearchForm(request.GET)
        if searchForm.is_valid():
            s = searchForm.cleaned_data['search']
            if errorModels is not None: # search errors only
                cset = Concept.search_text(s).filter(isError=True)
            else: # search correct concepts only
                cset = Concept.search_text(s).filter(isError=False)
                wset = Lesson.search_sourceDB(s)
            conceptForm = NewConceptForm() # let user define new concept
    if conceptForm:
        set_crispy_action(request.path, conceptForm)
    basePath = get_relative_url(request.path)
    kwargs.update(dict(cset=cset, actionTarget=request.path, msg=msg,
                       searchForm=searchForm, wset=wset, navTabs=navTabs,
                       toTable=toTable, fromTable=fromTable,
                       conceptForm=conceptForm, conceptLinks=conceptLinks,
                       actionLabel=actionLabel, pageTitle=pageTitle,
                       basePath=basePath, errorModels=errorModels))
    return render(request, 'ct/concepts.html', kwargs)


def edit_concept(request, course_id, unit_id, concept_id):
    concept = get_object_or_404(Concept, pk=concept_id)
    navTabs = concept_tabs(request.path, 'Edit')
    if request.user == concept.addedBy:
        titleform = ConceptForm(instance=concept)
        if request.method == 'POST':
            if 'title' in request.POST:
                titleform = ConceptForm(request.POST, instance=concept)
                if titleform.is_valid():
                    titleform.save()
            elif request.POST.get('task') == 'delete':
                concept.delete()
                return HttpResponseRedirect(reverse('ct:concepts'))
        set_crispy_action(request.path, titleform)
    else:
        titleform = None
    return render(request, 'ct/edit_concept.html',
                  dict(actionTarget=request.path, concept=concept,
                       atime=display_datetime(concept.atime),
                       titleform=titleform, navTabs=navTabs))

def get_relative_url(path, stem=-4, tail=-2, extension=()):
    'construct URL from desired stem and tail + optional extension'
    l = path.split('/')
    out = l[:stem]
    if tail:
        out += l[tail:]
    if extension:
        out += extension
    return '/'.join(out)

def make_tabs(path, current, tabs, stem=-2, tail=None):
    l = path.split('/')
    out = l[:stem]
    if tail:
        out += l[tail:]
    outTabs = []
    for label in tabs:
        if label == current:
            url = '#%sTabDiv' % label
        else:
            url = '/'.join(out + [label.lower(), ''])
        outTabs.append((label, url))
    return outTabs

def concept_tabs(path, current,
                 tabs=('Lessons', 'Concepts', 'Errors', 'Edit'), **kwargs):
    return make_tabs(path, current, tabs, **kwargs)

def lesson_tabs(path, current,
                 tabs=('Concepts', 'Errors', 'Edit'), **kwargs):
    return make_tabs(path, current, tabs, **kwargs)
    

class ConceptLinkTable(object):
    def __init__(self, data=(), formClass=ConceptLinkForm, noEdit=False,
                 **kwargs):
        self.formClass = formClass
        self.noEdit = noEdit
        self.data = []
        for cl in data:
            self.append(cl)
        for k,v in kwargs.items():
            setattr(self, k, v)
    def append(self, cl):
        self.data.append((cl, self.formClass(instance=cl)))
    def replace(self, cl, clform):
        for i,t in enumerate(self.data):
            if t[0] == cl:
                self.data[i] = (cl, clform)
    def remove(self, cl):
        for i,t in enumerate(self.data):
            if t[0] == cl:
                del self.data[i]
                return True
    def move_between_tables(self, cl, other):
        if self.remove(cl):
            other.append(cl)
        elif other.remove(cl):
            self.append(cl)

@login_required
def unit_concepts(request, course_id, unit_id):
    unit = get_object_or_404(Unit, pk=unit_id)
    navTabs = (('Lessons', '/lessons'), ('Edit', '/edit'),)
    cLinks = ConceptLink.objects.filter(Q(lesson__unitlesson__unit=unit) &
                                        ~Q(lesson__unitlesson__kind=
                                           UnitLesson.MISUNDERSTANDS))
    clTable = ConceptLinkTable(cLinks, noEdit=True,
                        headers=('This courselet...', 'Concept'),
                        title='Concepts Linked to this Courselet')
    r = _concepts(request, '''To add a concept to this courselet, start by
    typing a search for relevant concepts. ''', conceptLinks=clTable,
                  pageTitle=unit.title, unit=unit, navTabs=navTabs)
    if isinstance(r, Concept):
        url = '%s%d/lessons/' % (request.path, r.id)
        return HttpResponseRedirect(url)
    return r

@login_required
def ul_concepts(request, course_id, unit_id, ul_id):
    unitLesson = get_object_or_404(UnitLesson, pk=ul_id)
    cLinks = ConceptLink.objects.filter(lesson=unitLesson.lesson)
    clTable = ConceptLinkTable(cLinks, headers=('This lesson...', 'Concept'),
                               title='Concepts Linked to this Lesson')
    r = _concepts(request, '''To add a concept to this lesson, start by
    typing a search for relevant concepts. ''', conceptLinks=clTable,
                  pageTitle=unitLesson.lesson.title, unit=unitLesson.unit)
    if isinstance(r, Concept):
        cl = unitLesson.lesson.conceptlink_set.create(concept=r,
                                                      addedBy=request.user)
        clTable.append(cl)
        return _concepts(request, '''Successfully added concept.
            Thank you!''', ignorePOST=True, conceptLinks=clTable,
                  pageTitle=unitLesson.lesson.title, unit=unitLesson.unit)
    return r

def concept_page_data(request, unit_id, concept_id, currentTab):
    unit = get_object_or_404(Unit, pk=unit_id)
    concept = get_object_or_404(Concept, pk=concept_id)
    navTabs = concept_tabs(request.path, currentTab)
    headText = md2html(concept.description)
    return unit, concept, navTabs, headText

@login_required
def concept_concepts(request, course_id, unit_id, concept_id):
    unit, concept, navTabs, headText = \
      concept_page_data(request, unit_id, concept_id, 'Concepts')
    toConcepts = concept.relatedTo.all()
    fromConcepts = concept.relatedFrom \
      .exclude(relationship=ConceptGraph.MISUNDERSTANDS)
    toTable = ConceptLinkTable(toConcepts, ConceptGraphForm,
                    headers=('This concept...', 'Related Concept'),
                    title='Concepts Linked from this Concept')
    fromTable = ConceptLinkTable(fromConcepts, ConceptGraphForm,
                    headers=('Related Concept', '...this concept',),
                    title='Concepts Linking to this Concept')
    r = _concepts(request, '''To add a concept link, start by
    typing a search for relevant concepts. ''', toTable=toTable,
                  fromTable=fromTable, pageTitle=concept.title, unit=unit,
                  headText=headText, navTabs=navTabs)
    if isinstance(r, Concept):
        cg = concept.relatedTo.create(toConcept=r, addedBy=request.user)
        toTable.append(cg)
        return _concepts(request, '''Successfully added concept.
            Thank you!''', ignorePOST=True, toTable=toTable,
            fromTable=fromTable, pageTitle=concept.title, unit=unit,
            headText=headText, navTabs=navTabs)
    return r


@login_required
def concept_errors(request, course_id, unit_id, concept_id):
    unit, concept, navTabs, headText = \
      concept_page_data(request, unit_id, concept_id, 'Errors')
    errorModels = list(concept.relatedFrom
      .filter(relationship=ConceptGraph.MISUNDERSTANDS))
    r = _concepts(request, '''To add a concept link, start by
    typing a search for relevant concepts. ''', errorModels=errorModels,
                  pageTitle=concept.title, unit=unit, headText=headText,
                  navTabs=navTabs)
    if isinstance(r, Concept):
        r.isError = True
        cg = concept.relatedFrom.create(fromConcept=r, addedBy=request.user,
                                    relationship=ConceptGraph.MISUNDERSTANDS)
        errorModels.append(cg)
        return _concepts(request, '''Successfully added error model.
            Thank you!''', ignorePOST=True, errorModels=errorModels,
            pageTitle=concept.title, unit=unit, headText=headText,
            navTabs=navTabs)
    return r


def _lessons(request, concept, msg='', ignorePOST=False, conceptLinks=None,
             pageTitle='Lessons', unit=None, actionLabel='Add to This Unit',
             navTabs=(), **kwargs):
    'search or create a Lesson'
    lessonForm = None
    searchForm = LessonSearchForm()
    lessonSet = ()
    if request.method == 'POST' and not ignorePOST:
        if 'clID' in request.POST:
            update_concept_link(request, conceptLinks)
        elif 'ulID' in request.POST:
            return UnitLesson.objects.get(pk=int(request.POST.get('ulID')))
        elif 'title' in request.POST: # create new lesson
            lessonForm = NewLessonForm(request.POST)
            if lessonForm.is_valid():
                lesson = lessonForm.save(commit=False)
                lesson.addedBy = request.user
                lesson.save_root(concept)
                return UnitLesson.create_from_lesson(lesson, unit)
        else:
            return 'please write POST error message'

    elif 'search' in request.GET:
        searchForm = LessonSearchForm(request.GET)
        if searchForm.is_valid():
            s = searchForm.cleaned_data['search']
            lessonSet = distinct_subset(UnitLesson.objects. 
              filter(Q(lesson__title__icontains=s) |
                     Q(lesson__text__icontains=s)))
            lessonForm = NewLessonForm()
    if lessonForm:
        set_crispy_action(request.path, lessonForm)
    basePath = get_relative_url(request.path)
    kwargs.update(dict(lessonSet=lessonSet, actionTarget=request.path, msg=msg,
                       searchForm=searchForm, navTabs=navTabs,
                       lessonForm=lessonForm, conceptLinks=conceptLinks,
                       actionLabel=actionLabel, pageTitle=pageTitle,
                       basePath=basePath))
    return render(request, 'ct/lessons.html', kwargs)

@login_required
def concept_lessons(request, course_id, unit_id, concept_id):
    unit, concept, navTabs, headText = \
      concept_page_data(request, unit_id, concept_id, 'Lessons')
    cLinks = ConceptLink.objects.filter(concept=concept)
    clTable = ConceptLinkTable(cLinks, headers=('Lesson', '...this concept'),
                               title='Lessons Linked to this Concept')
    r = _lessons(request, concept,
       '''To add a lesson to this concept, start by
       typing a search for relevant lessons. ''', conceptLinks=clTable,
                  pageTitle=concept.title, unit=unit, headText=headText,
                  navTabs=navTabs)
    if isinstance(r, UnitLesson):
        if r.unit != unit: # copy from another unit
            r = r.copy()
        elif r.lesson.kind == Lesson.ORCT_QUESTION:
            if r.unitlesson_set.filter(kind=UnitLesson.MISUNDERSTANDS) \
                                      .count() == 0: # add error models
                for cg in concept.relatedFrom \
                    .filter(relationship=ConceptGraph.MISUNDERSTANDS):
                    lesson = Lesson.objects \
                      .get(conceptlink__concept=cg.fromConcept,
                           conceptlink__relationship=ConceptLink.IS)
                    UnitLesson.create_from_lesson(lesson, unit,
                            kind=UnitLesson.MISUNDERSTANDS, parent=r)
            if r.unitlesson_set.filter(kind=UnitLesson.ANSWERS).count() == 0:
                answer = Lesson(title='Answer', text='write an answer',
                                addedBy=r.addedBy, kind=Lesson.ANSWER)
                answer.save_root()
                ul = UnitLesson.create_from_lesson(answer, unit,
                            kind=UnitLesson.ANSWERS, parent=r)
                return HttpResponseRedirect(reverse('ct:edit_lesson',
                                args=(course_id, unit_id, ul.id,)))
        cl = r.lesson.conceptlink_set.get()
        clTable.append(cl)
        return _lessons(request, concept, '''Successfully added lesson.
            Thank you!''', ignorePOST=True, conceptLinks=clTable,
            pageTitle=concept.title, unit=unit, headText=headText,
            navTabs=navTabs)
    return r


def edit_lesson(request, course_id, unit_id, ul_id):
    ul = get_object_or_404(UnitLesson, pk=ul_id)
    navTabs = lesson_tabs(request.path, 'Edit')
    if request.user == ul.addedBy:
        titleform = LessonForm(instance=ul.lesson)
        if request.method == 'POST':
            if 'title' in request.POST:
                titleform = LessonForm(request.POST, instance=ul.lesson)
                if titleform.is_valid():
                    titleform.save()
            elif request.POST.get('task') == 'delete':
                ul.delete()
                return HttpResponseRedirect(reverse('ct:concepts'))
        set_crispy_action(request.path, titleform)
    else:
        titleform = None
    return render(request, 'ct/edit_lesson.html',
                  dict(actionTarget=request.path, unitLesson=ul,
                       atime=display_datetime(ul.atime),
                       titleform=titleform, navTabs=navTabs))

###########################################################
# student UI for courses

def get_live_sessions(request):
    return () # !! implement live session finding here!!
    ## return LiveSession.objects.filter(course__role__user=request.user,
    ##                                   endTime__isnull=True)
#                                      course__role__role=Role.ENROLLED)

@login_required
def main_page(request):
    'generic home page'
    fsmStack = FSMStack(request)
    if request.method == 'POST':
        if 'liveID' in request.POST:
            liveID = int(request.POST['liveID'])
            liveSession = LiveSession.objects.get(pk=liveID) # make sure it exists
            return fsmStack.push(request, 'liveStudent',
                  dict(liveSession=liveSession,
                       liveQuestion=liveSession.liveQuestion))
        elif request.POST.get('task') == 'liveend': # end live session
            rm_live_user(request, fsmStack.state.liveSession)
            return fsmStack.pop(request)
    return render(request, 'ct/index.html',
                  dict(liveSessions=get_live_sessions(request),
                       actionTarget=request.path, fsmStack=fsmStack))

def course_study(request, course_id):
    'generic page for student course view'
    course = get_object_or_404(Course, pk=course_id)
    target = redirect_live(request)
    if target:
        return target
    return render(request, 'ct/course_study.html',
                  dict(course=course, actionTarget=request.path))

def redirect_live(request, default=None):
    'redirect student to live exercise if ongoing'
    response, liveSession = live_response(request)
    if not liveSession:
        return default
    elif isinstance(response, HttpResponseRedirect):
        return response
    else:
        return HttpResponseRedirect(reverse('ct:home'))
        
            


###############################################################
# utility functions

def set_crispy_action(actionTarget, *forms):
    'set the form.helper.form_action for one or more forms'
    for form in forms:
        form.helper.form_action = actionTarget
    
    
############################################################
# NOT USED... JUST EXPERIMENTAL

@login_required
def remedy_page(request, em_id):
    em = get_object_or_404(ErrorModel, pk=em_id)
    return render_remedy_form(request, em)

def render_remedy_form(request, em, **context):
    context.update(dict(errorModel=em, qtext=md2html(em.question.qtext),
                        answer=md2html(em.question.answer)))
    return render(request, 'ct/remedy.html', context)

@login_required
def submit_remedy(request, em_id):
    em = get_object_or_404(ErrorModel, pk=em_id)
    try:
        remediation = request.POST['remediation'].strip()
        counterExample = request.POST['counterExample'].strip()
        if not remediation or not counterExample:
            raise KeyError
    except KeyError:
        return render_remedy_form(request, em, 
                   remediation=request.POST.get('remediation', ''),
                   counterExample=request.POST.get('counterExample', ''),
                   error_message='You must give both a remediation and a counter-example.')
    em.remediation_set.create(atime=timezone.now(), remediation=remediation,
                              counterExample=counterExample, 
                              author=request.user)
    return HttpResponseRedirect('/ct')

@login_required
def glossary_page(request, glossary_id):
    g = get_object_or_404(Glossary, pk=glossary_id)
    return render_glossary_form(request, g)

def render_glossary_form(request, g, **context):
    uniqueTerms = set()
    mine = []
    for v in g.vocabulary_set.all():  # condense to unique terms
        uniqueTerms.add(v.term)
        if v.author.id == request.user.id:
            mine.append(v)
    existingTerms = list(uniqueTerms)
    existingTerms.sort()
    existingTerms = ', '.join(existingTerms)
    mine.sort(lambda a,b:cmp(a.term,b.term))
    context.update(dict(glossary=g, existingTerms=existingTerms,
                        vocabulary=mine))
    return render(request, 'ct/write_glossary.html', context)

@login_required
def submit_term(request, glossary_id):
    g = get_object_or_404(Glossary, pk=glossary_id)
    try:
        term = request.POST['term'].strip()
        definition = request.POST['definition'].strip()
        if not term or not definition:
            raise KeyError
    except KeyError:
        return render_glossary_form(request, g, 
                   term=request.POST.get('term', ''),
                   definition=request.POST.get('definition', ''),
                   error_message='You must give both a term and a definition.')
    g.vocabulary_set.create(atime=timezone.now(), term=term,
                            definition=definition, author=request.user)
    return HttpResponseRedirect(reverse('ct:write_glossary', args=(g.id,)))
