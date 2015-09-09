import os

from django.test import LiveServerTestCase
from django.test.utils import override_settings, skipUnless
from opensubmit.tests.cases import StudentTestCase
from django.contrib.auth.models import User

from opensubmit.models import TestMachine, SubmissionTestResult, Submission

from opensubmit import executor 

class ExecutorTestCase(StudentTestCase, LiveServerTestCase):
    def setUp(self):
        super(ExecutorTestCase, self).setUp()

    def _registerExecutor(self):
        executor.send_config("opensubmit/tests/executor.cfg")
        return TestMachine.objects.order_by('-last_contact')[0]

    def _runExecutor(self):
        executor.run("opensubmit/tests/executor.cfg")

    def testRegisterExecutorExplicit(self):
        machine_count = TestMachine.objects.all().count()
        assert(self._registerExecutor().pk)
        self.assertEquals(machine_count+1, TestMachine.objects.all().count())

    def testRunRequestFromUnknownMachine(self):
        # This is expected to trigger a register action request from the server
        self.assertNotEquals(0, self._runExecutor())

    @override_settings(JOB_EXECUTOR_SECRET='foo')
    def testInvalidSecret(self):
        self.assertNotEquals(0, self._runExecutor())

    def testEverythingAlreadyTested(self):
        self.createValidatedSubmission(self.current_user)
        assert(self._registerExecutor().pk)
        self.assertEquals(0, self._runExecutor())

    def testCompileTest(self):
        self.sub = self.createValidatableSubmission(self.current_user) 
        test_machine = self._registerExecutor()
        self.sub.assignment.test_machines.add(test_machine)
        self.assertEquals(0, self._runExecutor())
        results = SubmissionTestResult.objects.filter(
            submission_file=self.sub.file_upload,
            kind=SubmissionTestResult.COMPILE_TEST
        )
        self.assertEquals(1, len(results))
        self.assertNotEquals(0, len(results[0].result))

    def testValidationTest(self):
        # We need a fully working compile run beforehand
        self.testCompileTest()
        self.assertEquals(0, self._runExecutor())
        results = SubmissionTestResult.objects.filter(
            submission_file=self.sub.file_upload,
            kind=SubmissionTestResult.VALIDITY_TEST
        )
        self.assertEquals(1, len(results))
        self.assertNotEquals(0, len(results[0].result))

    def testFullTest(self):
        # We need a fully working validation run beforehand
        self.testValidationTest()
        self.assertEquals(0, self._runExecutor())
        results = SubmissionTestResult.objects.filter(
            submission_file=self.sub.file_upload,
            kind=SubmissionTestResult.FULL_TEST
        )
        self.assertEquals(1, len(results))
        self.assertNotEquals(0, len(results[0].result))

    def testAssignmentSpecificTestMachine(self):
        # Register two test machines T1 and T2
        real_machine = self._registerExecutor()        
        fake_machine = TestMachine(host="127.0.0.2")
        fake_machine.save()
        # Assign each of them to a different assignment A1 and A2
        self.openAssignment.test_machines.add(real_machine)
        self.validatedAssignment.test_machines.add(fake_machine)
        # Produce submissions for the assignments
        sub1 = Submission(
            assignment=self.validatedAssignment,
            submitter=self.current_user.user,
            state=Submission.TEST_COMPILE_PENDING,
            file_upload=self.createSubmissionFile()
        )
        sub1.save()
        # Real executor should not take care of this submission, since it is not for 'openAssignment'
        old_sub1_state = sub1.state
        self.assertEquals(0, self._runExecutor())    
        sub1 = Submission.objects.get(pk=sub1.pk)
        self.assertEquals(old_sub1_state, sub1.state)
        self.assertEquals(0, self._runExecutor())        

        # T2 should only take care of A2
