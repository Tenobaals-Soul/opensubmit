import StringIO, zipfile

from django import http
from django.contrib.admin.sites import AdminSite
from opensubmit.tests.cases import SubmitTutorTestCase, MockRequest
from opensubmit.models import Assignment, Course, SubmissionFile, Submission, GradingScheme
from opensubmit.admin.course import CourseAdmin

class TutorACLTestCase(SubmitTutorTestCase):

    def setUp(self):
        super(TutorACLTestCase, self).setUp()

    def testCanUseTeacherBackend(self):
        response = self.c.get('/teacher/opensubmit/submission/')
        self.assertEquals(response.status_code, 200)        

    def testCannotUseAdminBackend(self):
        response = self.c.get('/admin/auth/user/')
        self.assertEquals(response.status_code, 403)        # 302: can access the model in principle, 403: can never access the app label

class BackendTestCase(TutorACLTestCase):
    '''
        Test different teacher backend functions.
    '''
    def testAssignmentBackend(self):
        from opensubmit.admin.assignment import AssignmentAdmin
        from opensubmit.admin.assignment import course as course_title
        assadm = AssignmentAdmin(Assignment, AdminSite())
        assignments_shown = assadm.get_queryset(self.request)
        for assignment in assignments_shown:
            self.assertEquals(assignment.course, self.course)
            self.assertEquals(course_title(assignment), self.course.title)

    def testCourseBackend(self):
        from opensubmit.admin.course import assignments as course_assignments
        courseadm = CourseAdmin(Course, AdminSite())
        num_courses = courseadm.get_queryset(self.request).count()
        self.assertEquals(num_courses, 1)
        ass_str_list = course_assignments(self.course)
        for ass in self.allAssignments:
            assert(ass.title in ass_str_list)

    def testGradingBackend(self):
        from opensubmit.admin.grading import means_passed, grading_schemes
        self.assertEquals(means_passed(self.passGrade), True)
        self.assertEquals(means_passed(self.failGrade), False)
        self.assertEquals(grading_schemes(self.passGrade), self.passFailGrading.title)

    def testGradingsListFromGradingScheme(self):
        from opensubmit.admin.gradingscheme import gradings
        gradings_list = gradings(self.passFailGrading)
        assert(self.passGrade.title in gradings_list)
        assert(self.failGrade.title in gradings_list)

    def testCourseListFromGradingScheme(self):
        from opensubmit.admin.gradingscheme import courses
        course_list = courses(self.passFailGrading)
        assert(self.course.title in course_list)

    def testSubmissionFileBackend(self):
        from opensubmit.admin.submissionfile import SubmissionFileAdmin
        subfileadm = SubmissionFileAdmin(SubmissionFile, AdminSite())
        files_shown = subfileadm.get_queryset(self.request).count()
        self.assertEquals(0, files_shown)

    def testGradingSchemeAdminRendering(self):
        from opensubmit.admin.gradingscheme import GradingSchemeAdmin
        gsadmin = GradingSchemeAdmin(GradingScheme, AdminSite())
        assert('GradingSchemeForm' in str(gsadmin.get_form(self.request))) 


class SubmissionBackendTestCase(TutorACLTestCase):
    '''
        Test submission-related teacher backend functions, which needs more setUp
        than the other teacher backend test cases.
    '''
    def setUp(self):
        from opensubmit.admin.submission import SubmissionAdmin
        super(SubmissionBackendTestCase, self).setUp()
        self.sub1 = self.createSubmission(self.current_user, self.openAssignment)
        self.sub2 = self.createSubmission(self.current_user, self.softDeadlinePassedAssignment)
        self.val_sub = self.createValidatedSubmission(self.current_user)
        self.all_submissions = [self.sub1, self.sub2, self.val_sub]
        self.submadm = SubmissionAdmin(Submission, AdminSite())

    def testAuthorsFromSubmission(self):
        from opensubmit.admin.submission import authors
        assert(self.current_user.user.first_name in authors(self.sub1))

    def testSubmissionBackend(self):
        submissions = self.submadm.get_queryset(self.request)
        self.assertSequenceEqual(submissions, self.all_submissions)

    # def testGetPerformanceResult(self):
    #     csv_response = self.submadm.getPerformanceResultsAction(self.request, Submission.objects.all())
    #     assert(csv_response.status_code == 200)
    #     assert('text/csv' in str(csv_response))   

    def testCloseAndNotify(self):
        from django.core import mail
        # Everything in status 'SUBMITTED', so no mail should be sent
        self.submadm.closeAndNotifyAction(self.request, Submission.objects.all())
        self.assertEquals(0, len(mail.outbox))
        # One mail should be sent
        self.sub1.state = Submission.GRADED
        self.sub1.save()
        self.sub2.state = Submission.GRADED
        self.sub2.save()        
        self.submadm.closeAndNotifyAction(self.request, Submission.objects.all())        
        self.assertEquals(2, len(mail.outbox))

    def testSetFullPendingAll(self):
        # Only one of the submission assignments has validation configured
        self.submadm.setFullPendingStateAction(self.request, Submission.objects.all())
        self.assertEquals(1, Submission.objects.filter(state=Submission.TEST_FULL_PENDING).count())

    def testSetFullPendingNoneMatching(self):
        # Only one of the submission assignments has validation configured
        self.submadm.setFullPendingStateAction(self.request, Submission.objects.filter(state=Submission.SUBMITTED))
        self.assertEquals(0, Submission.objects.filter(state=Submission.TEST_FULL_PENDING).count())

    def testSetInitialState(self):
        self.submadm.setInitialStateAction(self.request, Submission.objects.all())
        self.assertEquals(2, Submission.objects.filter(state=Submission.SUBMITTED).count())

    def testGradingNoteIndicator(self):
        from opensubmit.admin.submission import grading_notes
        self.assertEquals(False, grading_notes(self.sub1))
        self.sub1.grading_notes = 'Your are a bad student.'
        self.sub1.save()
        self.assertEquals(True, grading_notes(self.sub1))

    def testGradingFileIndicator(self):
        from django.core.files import File as DjangoFile
        from opensubmit.admin.submission import grading_file
        self.assertEquals(False, grading_file(self.sub1))
        self.sub1.grading_file = DjangoFile(open(__file__), unicode("grading_file.txt"))  
        self.sub1.save()
        self.assertEquals(True, grading_file(self.sub1))

    def testStateFilter(self):
        from opensubmit.admin.submission import SubmissionStateFilter
        submfilter = SubmissionStateFilter(self.request, {'statefilter': 'tobegraded'}, Submission, None)
        for sub in submfilter.queryset(self.request, Submission.objects.all()):
            assert(sub in self.all_submissions)
        graded_count = SubmissionStateFilter(self.request, {'statefilter': 'graded'}, Submission, None).queryset(self.request, Submission.objects.all()).count()
        self.assertEquals(graded_count, 0)

    def testAssignmentFilter(self):
        from opensubmit.admin.submission import SubmissionAssignmentFilter
        submfilter = SubmissionAssignmentFilter(self.request, {'assignmentfilter': self.sub1.assignment.pk}, Submission, None)
        sublist = submfilter.queryset(self.request, Submission.objects.all()).values_list('pk', flat=True)      
        self.assertSequenceEqual(sublist, [self.sub1.pk])


    def testCourseFilter(self):
        from opensubmit.admin.submission import SubmissionCourseFilter
        submfilter = SubmissionCourseFilter(self.request, {'coursefilter': self.course.pk}, Submission, None)
        subcount = submfilter.queryset(self.request, Submission.objects.all()).count()      
        self.assertEquals(subcount, len(self.all_submissions))

    def testGradingTableView(self):
        response = self.c.get('/course/%u/gradingtable/'%self.course.pk)
        self.assertEquals(response.status_code, 200)

    def testArchiveView(self):
        response = self.c.get('/course/%u/archive/'%self.course.pk)
        self.assertEquals(response.status_code, 200)
        # Test if the download is really a ZIP file
        f = StringIO.StringIO(response.content)
        zipped_file = zipfile.ZipFile(f, 'r')
        try:
            # Check ZIP file validity
            self.assertIsNone(zipped_file.testzip())
            # Try to find a file some student stored in a sub-folder on it's own, targets #18
            found_stud_subfile = False
            for entry in zipped_file.filelist:
                if "student_folder/folder_in_folder/student_file_in_subfolder" in entry.filename:
                    found_stud_subfile = True
            assert(found_stud_subfile)
        finally:
            zipped_file.close()
            f.close()        

