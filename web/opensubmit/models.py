import os

import logging
import string
import unicodedata
import zipfile, tarfile
from datetime import datetime, timedelta


from django.db import models, transaction
from django.contrib.auth.models import User, Group
from django.utils import timezone
from django.core.mail import send_mail, EmailMessage
from django.core.exceptions import SuspiciousOperation
from django.core.urlresolvers import reverse
from settings import MAIN_URL, MEDIA_ROOT
from itertools import chain

from opensubmit import settings

logger = logging.getLogger('OpenSubmit')


def upload_path(instance, filename):
    '''
        Sanitize the user-provided file name, add timestamp for uniqness.
    '''

    filename = filename.replace(" ", "_")
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').lower()
    return os.path.join(str(timezone.now().date().isoformat()), filename)


class Grading(models.Model):
    title = models.CharField(max_length=20, help_text="The title of the grade, such as 'A', 'B', 'Pass', or 'Fail'.")
    means_passed = models.BooleanField(default=True, help_text="Students are informed about their pass or fail in the assignment, based on this flag in their given grade.")

    class Meta:
        app_label = 'opensubmit'

    def __unicode__(self):
        return unicode(self.title)


class GradingScheme(models.Model):
    title = models.CharField(max_length=200, help_text="Choose a directly understandable name, such as 'ECTS' or 'Pass / Fail'.")
    gradings = models.ManyToManyField(Grading, related_name='schemes', help_text="The list of gradings that form this grading scheme.")

    class Meta:
        app_label = 'opensubmit'

    def __unicode__(self):
        return unicode(self.title)


class Course(models.Model):
    title = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True, editable=False)
    owner = models.ForeignKey(User, related_name='courses', help_text="Only this user can change the course details and create new assignments.")
    tutors = models.ManyToManyField(User, blank=True, related_name='courses_tutoring', help_text="These users can edit / grade submissions for the course.")
    homepage = models.URLField(max_length=200, verbose_name="Course description link")
    active = models.BooleanField(default=True, help_text="Only assignments and submissions of active courses are shown to students and tutors. Use this flag for archiving past courses.")
    max_authors = models.PositiveSmallIntegerField(default=1, help_text="Maximum number of authors (= group size) for assignments in this course.")

    class Meta:
        app_label = 'opensubmit'

    def __unicode__(self):
        return unicode(self.title)

    def is_owner(self, user):
        return user == self.owner

    def is_tutor(self, user):
        return self.tutors.filter(pk=user.pk).exists()

    def is_owner_or_tutor(self, user):
        return self.is_owner(user) or self.is_tutor(user)

    def is_visible(self, user=None):
        if user:
            if self.is_owner_or_tutor(user):
                return True

        if not self.active:
            return False

        return True

    def open_assignments(self):
        qs = Assignment.objects.filter(hard_deadline__gt=timezone.now())
        qs = qs.filter(publish_at__lt=timezone.now())
        qs = qs.filter(course=self)
        qs = qs.order_by('soft_deadline').order_by('hard_deadline').order_by('title')
        return qs

    def gradable_submissions(self):
        qs = self.valid_submissions()
        qs = qs.filter(state__in=[Submission.GRADING_IN_PROGRESS, Submission.SUBMITTED_TESTED, Submission.TEST_FULL_FAILED, Submission.SUBMITTED])
        return qs

    def graded_submissions(self):
        qs = self.valid_submissions().filter(state__in=[Submission.GRADED])
        return qs

    def authors(self):
        qs = self.valid_submissions().values_list('authors',flat=True).distinct()
        return qs

    def valid_submissions(self):
        qs = Submission.objects.filter(assignment__course=self).exclude(state=Submission.WITHDRAWN)
        return qs

class TestMachine(models.Model):
    host = models.CharField(null=True, max_length=50, help_text="UUID of the test machine, independent from IP address.")
    address = models.CharField(null=True,  max_length=50, help_text="Internal IP address of the test machine, at the time of registration.")
    last_contact = models.DateTimeField(editable=False, default=timezone.now)
    config = models.TextField(null=True, help_text="Host configuration, as shown to the students, in JSON format.")

    class Meta:
        app_label = 'opensubmit'

    def __unicode__(self):
        if self.address:
            return unicode(self.address)
        else:
            return "Test Machine %u"%self.pk


class Assignment(models.Model):
    '''
        An assignment for which students can submit their solution.
    '''

    title = models.CharField(max_length=200)
    course = models.ForeignKey(Course, related_name='assignments')
    download = models.URLField(max_length=200, verbose_name="Link for assignment description")
    created = models.DateTimeField(auto_now_add=True, editable=False)
    gradingScheme = models.ForeignKey(GradingScheme, related_name="assignments", verbose_name="grading scheme")
    publish_at = models.DateTimeField(default=timezone.now)
    soft_deadline = models.DateTimeField(blank=True, null=True, help_text="Deadline shown to students. After this point in time, submissions are still possible. Leave empty for only using a hard deadline.")
    hard_deadline = models.DateTimeField(help_text="Deadline after which submissions are no longer possible.")
    has_attachment = models.BooleanField(default=False, verbose_name="Student file upload ?", help_text="Activate this if the students must upload a (document / ZIP /TGZ) file as solution. Otherwise, they can only fill the notes field.")
    attachment_test_timeout = models.IntegerField(default=30, verbose_name="Timout for tests", help_text="Timeout (in seconds) after which the compilation / validation test / full test is cancelled. The submission is marked as invalid in this case. Intended for student code with deadlocks.")
    attachment_test_compile = models.BooleanField(default=False, verbose_name="Compile test ?", help_text="If activated, the student upload is uncompressed and 'make' is executed on one of the test machines.")
    attachment_test_validity = models.FileField(upload_to="testscripts", blank=True, null=True, verbose_name='Validation script', help_text="If given, the student upload is uncompressed, compiled and the script is executed for it on a test machine. Student submissions are marked as valid if this script was successful.")
    validity_script_download = models.BooleanField(default=False, verbose_name='Download of validation script ?', help_text='If activated, the students can download the validation script for offline analysis.')
    attachment_test_full = models.FileField(upload_to="testscripts", blank=True, null=True, verbose_name='Full test script', help_text='Same as the validation script, but executed AFTER the hard deadline to determine final grading criterias for the submission. Results are not shown to students.')
    test_machines = models.ManyToManyField(TestMachine, blank=True, related_name="assignments", help_text="The test machines that will take care of submissions for this assignment.")

    class Meta:
        app_label = 'opensubmit'

    def gradable_submissions(self):
        qs = self.valid_submissions()
        qs = qs.filter(state__in=[Submission.GRADING_IN_PROGRESS, Submission.SUBMITTED_TESTED, Submission.TEST_FULL_FAILED, Submission.SUBMITTED])
        return qs

    def grading_unfinished_submissions(self):
        qs = self.valid_submissions()
        qs = qs.filter(state__in=[Submission.GRADING_IN_PROGRESS])
        return qs

    def graded_submissions(self):
        qs = self.valid_submissions().filter(state__in=[Submission.GRADED])
        return qs

    def authors(self):
        qs = self.valid_submissions().values_list('authors',flat=True).distinct()
        return qs

    def valid_submissions(self):
        qs = self.submissions.exclude(state=Submission.WITHDRAWN)
        return qs

    def has_perf_results(self):
        '''
            Figure out if any submission for this assignment has performance data being available.
        '''
        num_results = SubmissionTestResult.objects.filter(perf_data__isnull=False).filter(submission_file__submissions__assignment=self).count()
        return num_results != 0

    def validity_test_url(self):
        '''
            Return absolute download URL for validity test script.
            Using reverse() seems to be broken with FORCE_SCRIPT in use, so we use direct URL formulation.
        '''
        if self.pk:
            return settings.MAIN_URL + "/download/%u/validity_testscript/secret=%s" % (self.pk, settings.JOB_EXECUTOR_SECRET)
        else:
            return None

    def full_test_url(self):
        '''
            Return absolute download URL for full test script.
            Using reverse() seems to be broken with FORCE_SCRIPT in use, so we use direct URL formulation.
        '''
        if self.pk:
            return settings.MAIN_URL + "/download/%u/full_testscript/secret=%s" % (self.pk, settings.JOB_EXECUTOR_SECRET)
        else:
            return None

    def has_validity_test(self):
        return str(self.attachment_test_validity).strip() != ""

    def has_full_test(self):
        return str(self.attachment_test_full).strip() != ""

    def attachment_is_tested(self):
        return self.attachment_test_compile is True or self.has_validity_test() or self.has_full_test()

    def __unicode__(self):
        return unicode(self.title)

    def can_create_submission(self, user=None):
        if user:
            if user.is_superuser:
                # Super users are allowed to submit after the deadline.
                return True
            if user is self.course.owner:
                # The course owner is allowed to submit after the deadline.
                return True
            if self.course.tutors.filter(pk=user.pk).exists():
                # Tutors are allowed to submit after the deadline.
                return True

        if self.hard_deadline < timezone.now():
            # Hard deadline has been reached.
            return False

        if self.publish_at > timezone.now():
            # The assignment has not yet been published.
            return False

        if user:
            if self.course not in user_courses(user):
                # The user is not enrolled in this assignment's course.
                return False

            if user.authored.filter(assignment=self).exclude(state=Submission.WITHDRAWN).count() > 0:
                # User already has a valid submission for this assignment.
                return False

        return True

    def authors_valid(self, authors=()):
        for author in authors:
            if not self.can_create_submission(author):
                return False

        return True

    def is_visible(self, user=None):
        if user:
            if self.course.is_owner_or_tutor(user):
                return True

        if not self.course.is_visible(user):
            return False

        if self.publish_at > timezone.now():
            return False

        return True


# monkey patch for getting better user name stringification
# User proxies did not make the job
# Django's custom user model feature would have needed to be introduced
# before the first syncdb, whcih does not work for existing installations
def user_unicode(self):
    if self.email:
        shortened = self.email.split('@')[0]
        return u'%s %s (%s@...)' % (self.first_name, self.last_name, shortened)
    elif self.first_name or self.last_name:
        return u'%s %s' % (self.first_name, self.last_name)
    elif self.username:
        return u'%s' % (self.username)
    else:
        return u'User %u' % (self.pk)
User.__unicode__ = user_unicode


class UserProfile(models.Model):
    user = models.OneToOneField(User, related_name='profile')
    student_id = models.CharField(max_length=30, blank=True, null=True)
    courses = models.ManyToManyField(Course, blank=True, related_name='participants', limit_choices_to={'active__exact': True})

    class Meta:
        app_label = 'opensubmit'

    def tutor_courses(self):
        '''
            Returns the list of courses this user is tutor or owner for.
        '''
        tutoring = self.user.courses_tutoring.all().filter(active__exact=True)
        owning = self.user.courses.all().filter(active__exact=True)
        result = (tutoring | owning).distinct()
        return result

def user_courses(user):
    '''
        Returns the list of courses this user is subscribed for.
    '''
    return UserProfile.objects.get(user=user).courses.filter(active__exact=True)

@transaction.atomic
def move_user_data(primary, secondary):
    '''
        Moves all submissions and other data linked to the secondary user into the primary user.
        Nothing is deleted here, we just modify foreign user keys.
    '''
    # Update all submission authorships of the secondary to the primary
    submissions = Submission.objects.filter(authors__id=secondary.pk)
    for subm in submissions:
        if subm.submitter == secondary:
            subm.submitter = primary;
        subm.authors.remove(secondary)
        subm.authors.add(primary)
        subm.save()
    # Transfer course registrations
    try:
        for course in secondary.profile.courses.all():
            primary.profile.courses.add(course)
            primary.profile.save()
    except UserProfile.DoesNotExist:
        # That's a database consistency problem, but he will go away anyway
        pass

class ValidSubmissionFileManager(models.Manager):
    '''
        A model manager used by SubmissionFile. It returns only submission files
        that were not replaced.
    '''
    def get_queryset(self):
        return super(ValidSubmissionFileManager, self).get_queryset().filter(replaced_by=None)


class SubmissionFile(models.Model):
    '''
        A file attachment for a student submission. File attachments may be replaced
        by the student, but we keep the original version for some NSA-style data gathering.
        The "fetched" field defines the time stamp when the file was fetched for
        checking by some executor. On result retrieval, this timestamp is emptied
        again, which allows to find 'stucked' executor jobs on the server side.
    '''

    attachment = models.FileField(upload_to=upload_path, verbose_name="File upload")
    fetched = models.DateTimeField(editable=False, null=True)
    replaced_by = models.ForeignKey('SubmissionFile', null=True, blank=True, editable=False)

    class Meta:
        app_label = 'opensubmit'

    def __unicode__(self):
        return unicode(self.attachment.name)

    def basename(self):
        return self.attachment.name[self.attachment.name.rfind('/') + 1:]

    def get_absolute_url(self):
        # To realize access protection for student files, we implement our own download method here.
        # This implies that the Apache media serving (MEDIA_URL) is disabled.
        assert(len(self.submissions.all()) > 0)
        return reverse('download', args=(self.submissions.all()[0].pk, 'attachment'))

    def get_preview_url(self):
        return reverse('preview', args=(self.submissions.all()[0].pk,))

    def absolute_path(self):
        return MEDIA_ROOT + "/" + self.attachment.name

    def is_executed(self):
        return self.fetched is not None

    def is_archive(self):
        '''
            Determines if the attachment is an archive.
        '''
        try:
            if zipfile.is_zipfile(self.attachment.path) or tarfile.is_tarfile(self.attachment.path):
                return True
        except:
            pass
        return False

    def archive_previews(self):
        '''
            Return preview on archive file content as dictionary.
            In order to avoid browser and web server trashing by the students, there is a size limit for the single files shown.
        '''
        MAX_PREVIEW_SIZE = 10000

        result = []
        if zipfile.is_zipfile(self.attachment.path):
            zf = zipfile.ZipFile(self.attachment.path, 'r')
            for zipinfo in zf.infolist():
                if zipinfo.file_size < MAX_PREVIEW_SIZE:
                    result.append({'name': zipinfo.filename, 'preview': zf.read(zipinfo)})
                else:
                    result.append({'name': zipinfo.filename+' (too large)'})
        elif tarfile.is_tarfile(self.attachment.path):
            tf = tarfile.open(self.attachment.path,'r')
            for tarinfo in tf.getmembers():
                if tarinfo.isfile():
                    if tarinfo.size < MAX_PREVIEW_SIZE:
                        result.append({'name': tarinfo.name, 'preview': tf.extractfile(tarinfo).read()})
                    else:
                        result.append({'name': tarinfo.name+' (too large)'})
        else:
            # single file
            f=open(self.attachment.path)
            fname = f.name[f.name.rfind(os.sep)+1:]
            result = [{'name': fname, 'preview': f.read()},]
        return result

    def test_result_dict(self):
        '''
            Create a compact data structure representation of all result
            types for this file.

            Returns a dictionary where the keys are the result types, and
            the values are dicts of all the other result information.
        '''
        list_of_dicts=self.test_results.all().values()
        return {entry['kind']: {'result':entry['result']} for entry in list_of_dicts}

    objects = models.Manager()
    valid_ones = ValidSubmissionFileManager()


class PendingStudentTestsManager(models.Manager):
    '''
        A model manager used by the Submission model. It returns a sorted list
        of executor work to be done that relates to compilation and
        validation test jobs for students.
        The basic approach is that compilation should happen before validation in FIFO order,
        under the assumption is that the time effort is increasing.
    '''

    def get_queryset(self):
        jobs = Submission.objects.filter(
            state__in=[ Submission.TEST_COMPILE_PENDING,
                        Submission.TEST_VALIDITY_PENDING]
            ).order_by('state').order_by('-modified')
        return jobs


class PendingFullTestsManager(models.Manager):
    '''
        A model manager used by the Submission model. It returns a sorted list
        of full test executor work to be done.
        The basic approach is that non-graded job validation wins over closed job
        re-evaluation triggered by the teachers,
        under the assumption is that the time effort is increasing.
    '''

    def get_queryset(self):
        jobs = Submission.objects.filter(
            state__in=[ Submission.TEST_FULL_PENDING,
                        Submission.CLOSED_TEST_FULL_PENDING]
            ).order_by('-state').order_by('-modified')
        return jobs

class Submission(models.Model):
    '''
        A student submission for an assignment.
    '''

    RECEIVED = 'R'                   # Only for initialization, this should never persist
    WITHDRAWN = 'W'                  # Withdrawn by the student
    SUBMITTED = 'S'                  # Submitted, no tests so far
    TEST_COMPILE_PENDING = 'PC'      # Submitted, compile test planned
    TEST_COMPILE_FAILED = 'FC'       # Submitted, compile test failed
    TEST_VALIDITY_PENDING = 'PV'     # Submitted, validity test planned
    TEST_VALIDITY_FAILED = 'FV'      # Submitted, validity test failed
    TEST_FULL_PENDING = 'PF'         # Submitted, full test planned
    TEST_FULL_FAILED = 'FF'          # Submitted, full test failed
    SUBMITTED_TESTED = 'ST'          # Submitted, all tests performed, grading planned
    GRADING_IN_PROGRESS = 'GP'       # Grading in progress, but not finished
    GRADED = 'G'                     # Graded, student notification not done
    CLOSED = 'C'                     # Graded, student notification done
    CLOSED_TEST_FULL_PENDING = 'CT'  # Keep grading status, full test planned
    STATES = (                       # States from the backend point of view
        (RECEIVED, 'Received'),
        (WITHDRAWN, 'Withdrawn'),
        (SUBMITTED, 'Submitted'),
        (TEST_COMPILE_PENDING, 'Compilation test pending'),
        (TEST_COMPILE_FAILED, 'Compilation test failed'),
        (TEST_VALIDITY_PENDING, 'Validity test pending'),
        (TEST_VALIDITY_FAILED, 'Validity test failed'),
        (TEST_FULL_PENDING, 'Full test pending'),
        (TEST_FULL_FAILED, 'All but full test passed, grading pending'),
        (SUBMITTED_TESTED, 'All tests passed, grading pending'),
        (GRADING_IN_PROGRESS, 'Grading not finished'),
        (GRADED, 'Grading finished'),
        (CLOSED, 'Closed, student notified'),
        (CLOSED_TEST_FULL_PENDING, 'Closed, full test pending')
    )
    STUDENT_STATES = (              # States from the student point of view
        (RECEIVED, 'Received'),
        (WITHDRAWN, 'Withdrawn'),
        (SUBMITTED, 'Waiting for grading'),
        (TEST_COMPILE_PENDING, 'Waiting for compilation test'),
        (TEST_COMPILE_FAILED, 'Compilation failed'),
        (TEST_VALIDITY_PENDING, 'Waiting for validation test'),
        (TEST_VALIDITY_FAILED, 'Validation failed'),
        (TEST_FULL_PENDING, 'Waiting for grading'),
        (TEST_FULL_FAILED, 'Waiting for grading'),
        (SUBMITTED_TESTED, 'Waiting for grading'),
        (GRADING_IN_PROGRESS, 'Waiting for grading'),
        (GRADED, 'Waiting for grading'),
        (CLOSED, 'Graded'),
        (CLOSED_TEST_FULL_PENDING, 'Graded')
    )

    assignment = models.ForeignKey(Assignment, related_name='submissions')
    submitter = models.ForeignKey(User, related_name='submitted')
    authors = models.ManyToManyField(User, related_name='authored') # includes also submitter, see submission_post_save() handler
    notes = models.TextField(max_length=200, blank=True)
    file_upload = models.ForeignKey(SubmissionFile, related_name='submissions', blank=True, null=True, verbose_name='New upload')
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, editable=False, blank=True, null=True)
    grading = models.ForeignKey(Grading, blank=True, null=True)
    grading_notes = models.TextField(max_length=1000, blank=True, null=True, help_text="Specific notes about the grading for this submission.")
    grading_file = models.FileField(upload_to=upload_path, blank=True, null=True, help_text="Additional information about the grading as file.")
    state = models.CharField(max_length=2, choices=STATES, default=RECEIVED)

    class Meta:
        app_label = 'opensubmit'

    def __unicode__(self):
        if self.pk:
            return unicode("%u" % (self.pk))
        else:
            return unicode("New Submission instance")

    def log(self, level, format_string, *args, **kwargs):
        level_mapping = {
            'CRITICAL': logging.CRITICAL,
            'ERROR': logging.ERROR,
            'WARNING': logging.WARNING,
            'INFO': logging.INFO,
            'DEBUG': logging.DEBUG,
            'NOTSET': logging.NOTSET,
        }
        level_numeric = level_mapping[level] if level in level_mapping else level_mapping['NOTSET']
        if self.pk:
            log_prefix = "<{} pk={}>".format(self.__class__.__name__, self.pk)
        else:
            log_prefix = "<{} new>".format(self.__class__.__name__)
        return logger.log(level_numeric, "{} {}".format(log_prefix, format_string.format(*args, **kwargs)))

    def can_modify(self, user=None):
        """Determines whether the submission can be modified.
        Returns a boolean value.
        The 'user' parameter is optional and additionally checks whether
        the given user is authorized to perform these actions.

        This function checks the submission states and assignment deadlines."""

        # The user must be authorized to commit these actions.
        if user and not self.user_can_modify(user):
            self.log('DEBUG', "Submission cannot be modified, user is not an authorized user ({!r} not in {!r})", user, self.authorized_users)
            return False

        # Modification of submissions, that are withdrawn, graded or currently being graded, is prohibited.
        if self.state in [self.WITHDRAWN, self.GRADED, self.GRADING_IN_PROGRESS, ]:
            self.log('DEBUG', "Submission cannot be modified, is in state '{}'", self.state)
            return False

        # Modification of closed submissions is prohibited.
        if self.is_closed():
            self.log('DEBUG', "Submission cannot be modified, is closed")
            return False

        # Submissions, that are executed right now, cannot be modified
        if self.state in [self.TEST_COMPILE_PENDING, self.TEST_VALIDITY_PENDING, self.TEST_FULL_PENDING, ]:
            if not self.file_upload:
                self.log('CRITICAL', "Submission is in invalid state! State is '{}', but there is no file uploaded!", self.state)
                raise AssertionError()
                return False
            if self.file_upload.is_executed():
                # The above call informs that the uploaded file is being executed, or execution has been completed.
                # Since the current state is 'PENDING', the execution cannot yet be completed.
                # Thus, the submitted file is being executed right now.
                return False

        # Submissions must belong to an assignment.
        if not self.assignment:
            self.log('CRITICAL', "Submission does not belong to an assignment!")
            raise AssertionError()

        # Submissions, that belong to an assignment where the hard deadline has passed,
        # cannot be modified.
        if timezone.now() > self.assignment.hard_deadline:
            self.log('DEBUG', "Submission cannot be modified - assignment's hard deadline has passed (hard deadline is: {})", self.assignment.hard_deadline)
            return False

        # The soft deadline has no effect (yet).
        if self.assignment.soft_deadline:
            if timezone.now() > self.assignment.soft_deadline:
                # The soft deadline has passed
                pass  # do nothing.

        #self.log('DEBUG', "Submission can be modified.")
        return True

    def can_withdraw(self, user=None):
        """Determines whether a submisison can be withdrawn.
        Returns a boolean value.

        Requires: can_modify.

        Currently, the conditions for modifications and withdrawal are the same."""
        return self.can_modify(user=user)

    def can_reupload(self, user=None):
        """Determines whether a submission can be re-uploaded.
        Returns a boolean value.

        Requires: can_modify.

        Re-uploads are allowed only when test executions have failed."""

        # Re-uploads are allowed only when test executions have failed.
        if self.state not in (self.TEST_COMPILE_FAILED, self.TEST_VALIDITY_FAILED, self.TEST_FULL_FAILED, ):
            return False

        # It must be allowed to modify the submission.
        if not self.can_modify(user=user):
            return False

        return True

    def user_can_modify(self, user):
        """Determines whether a user is allowed to modify a specific submission in general.
        Returns a boolean value.

        A user is authorized when he is part of the authorized users (submitter and authors)."""
        return user in self.authorized_users

    @property
    def authorized_users(self):
        """Returns a list of all authorized users (submitter and additional authors)."""
        return [self.submitter, ] + list(self.authors.all())

    def is_withdrawn(self):
        return self.state == self.WITHDRAWN

    def is_closed(self):
        return self.state in [self.CLOSED, self.CLOSED_TEST_FULL_PENDING]

    def show_grading(self):
        return self.is_closed()

    def get_initial_state(self):
        '''
            Return first state for this submission after upload,
            which depends on the kind of assignment.
        '''
        if not self.assignment.attachment_is_tested():
            return Submission.SUBMITTED
        else:
            if self.assignment.attachment_test_compile:
                return Submission.TEST_COMPILE_PENDING
            elif self.assignment.attachment_test_validity:
                return Submission.TEST_VALIDITY_PENDING
            elif self.assignment.attachment_test_full:
                return Submission.TEST_FULL_PENDING

    def state_for_students(self):
        '''
            Return human-readable description of current state for students.
        '''
        return dict(self.STUDENT_STATES)[self.state]

    def state_for_tutors(self):
        '''
            Return human-readable description of current state for tutors.
        '''
        return dict(self.STATES)[self.state]

    def grading_file_url(self):
        # to implement access protection, we implement our own download
        # this implies that the Apache media serving is disabled
        return reverse('download', args=(self.pk, 'grading_file', ))

    objects = models.Manager()
    pending_student_tests = PendingStudentTestsManager()
    pending_full_tests = PendingFullTestsManager()

    def _save_test_result(self, machine, text, kind, perf_data):
        result = SubmissionTestResult(
            result=text,
            machine=machine,
            kind=kind,
            perf_data=perf_data)
        self.file_upload.test_results.add(result)

    def _get_test_result(self, kind):
        try:
            return self.file_upload.test_results.filter(kind=kind).order_by('-created')[0]
        except:
            return None

    def save_fetch_date(self):
        self.file_upload.fetched = datetime.now()
        self.file_upload.save()

    def get_fetch_date(self):
        return self.file_upload.fetched

    def clean_fetch_date(self):
        self.file_upload.fetched = None
        self.file_upload.save()

    def save_compile_result(self, machine, text):
        self._save_test_result(machine, text, SubmissionTestResult.COMPILE_TEST, None)

    def save_validation_result(self, machine, text, perf_data):
        self._save_test_result(machine, text, SubmissionTestResult.VALIDITY_TEST, perf_data)

    def save_fulltest_result(self, machine, text, perf_data):
        self._save_test_result(machine, text, SubmissionTestResult.FULL_TEST, perf_data)

    def get_compile_result(self):
        '''
            Return the most recent compile result object for this submission.
        '''
        return self._get_test_result(SubmissionTestResult.COMPILE_TEST)

    def get_validation_result(self):
        '''
            Return the most recent validity test result object for this submission.
        '''
        return self._get_test_result(SubmissionTestResult.VALIDITY_TEST)

    def get_fulltest_result(self):
        '''
            Return the most recent full test result object for this submission.
        '''
        return self._get_test_result(SubmissionTestResult.FULL_TEST)

class SubmissionTestResult(models.Model):
    '''
        An executor test result for a given submission file.
    '''

    COMPILE_TEST = 'c'
    VALIDITY_TEST = 'v'
    FULL_TEST = 'f'
    JOB_TYPES = (
        (COMPILE_TEST, 'Compilation test'),
        (VALIDITY_TEST, 'Validation test'),
        (FULL_TEST, 'Full test')
    )
    submission_file = models.ForeignKey(SubmissionFile, related_name="test_results")
    machine = models.ForeignKey(TestMachine, related_name="test_results")
    created = models.DateTimeField(auto_now_add=True, editable=False)
    result = models.TextField(null=True, blank=True)
    kind = models.CharField(max_length=2, choices=JOB_TYPES)
    perf_data = models.TextField(null=True, blank=True)

    class Meta:
        app_label = 'opensubmit'

# to avoid cyclic dependencies, we keep it in the models.py
# we hand-in explicitely about which new state we want to inform, since this may not be reflected
# in the model at the moment
def inform_student(submission, state):
    # we cannot send eMail on SUBMITTED_TESTED, since this may have been triggered by test repitition in the backend
    if state == Submission.TEST_COMPILE_FAILED:
        subject = 'Warning: Your submission did not pass the compilation test'
        message = u'Hi,\n\nthis is a short notice that your submission for "%s" in "%s" did not pass the automated compilation test. You need to update the uploaded files for a valid submission.\n\n Further information can be found at %s.\n\n'
        message = message % (submission.assignment, submission.assignment.course, MAIN_URL)

    elif state == Submission.TEST_VALIDITY_FAILED:
        subject = 'Warning: Your submission did not pass the validation test'
        message = u'Hi,\n\nthis is a short notice that your submission for "%s" in "%s" did not pass the automated validation test. You need to update the uploaded files for a valid submission.\n\n Further information can be found at %s.\n\n'
        message = message % (submission.assignment, submission.assignment.course, MAIN_URL)

    elif state == Submission.CLOSED:
        subject = 'Grading completed'
        message = u'Hi,\n\nthis is a short notice that your submission for "%s" in "%s" was graded.\n\n Further information can be found at %s.\n\n'
        message = message % (submission.assignment, submission.assignment.course, MAIN_URL)
    else:
        return

    subject = "[%s] %s" % (submission.assignment.course, subject)
    from_email = submission.assignment.course.owner.email
    recipients = submission.authors.values_list('email', flat=True).distinct().order_by('email')
    # send student email with BCC to course owner.
    # TODO: This might be configurable later
    # email = EmailMessage(subject, message, from_email, recipients, [submission.assignment.course.owner.email])
    email = EmailMessage(subject, message, from_email, recipients)
    email.send(fail_silently=True)


# to avoid cyclic dependencies, we keep it in the models.py
def inform_course_owner(request, submission):
    if submission.state == Submission.WITHDRAWN:
        subject = "Submission withdrawn"
        message = "Withdrawn solution %u for '%s'" % (submission.pk, submission.assignment)

    elif submission.state == Submission.SUBMITTED:
        subject = "Submission ready for grading"
        message = "Solution for '%s' that is ready for grading." % (submission.assignment)

    elif submission.state == Submission.SUBMITTED_TESTED:
        subject = "Submission tested and ready for grading"
        message = "Solution for '%s' that was tested and is ready for grading." % (submission.assignment)

    else:
        subject = "Submission changed state"
        message = "Submission has now the state '%s'." % (submission.STATES[submission.state])

    from_email = submission.assignment.course.owner.email
    recipients = [submission.assignment.course.owner.email]
    # TODO: Make this configurable, some course owners got annoyed by this
    # send_mail(subject, message, from_email, recipients, fail_silently=True)


def db_fixes(user):
    '''
    This is a monkey patch function called after login, which allows to deal with
    schema change issues I was too lazy to formulate in a South script.
    It is also the easiest alternative to a User instance post_save() handler.
    '''
    # Fix users that already exist and never got a user profile attached
    # This may be legacy users after the v0.28 introduction of UserProfile,
    # or users accounts that were created by the OpenID library or the admin.
    # TODO: The latter two belong into a User post_save handler. If we have this,
    #       then this code becomes obsolete for fresh installations.
    #
    # Users should start with all courses being visible, which was the behavior until v0.27
    profile, created = UserProfile.objects.get_or_create(user=user)
    if created:
        profile.courses = Course.objects.all()
        profile.save()


def open_assignments(user):
    ''' Returns the list of open assignments from the viewpoint of this user.
        The caller can request the information under consideration of existing submission
        from this user (the dashboard case) or under ignorance of them (the signal handler case).
    '''
    qs = Assignment.objects.filter(hard_deadline__gt=timezone.now())
    qs = qs.filter(publish_at__lt=timezone.now())
    qs = qs.filter(course__in=user_courses(user))
    qs = qs.order_by('soft_deadline').order_by('hard_deadline').order_by('title')
    waiting_for_action = [subm.assignment for subm in user.authored.all().exclude(state=Submission.WITHDRAWN)]
    return [ass for ass in qs if ass not in waiting_for_action]
