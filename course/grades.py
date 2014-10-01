# -*- coding: utf-8 -*-

from __future__ import division

__copyright__ = "Copyright (C) 2014 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import re

from django.shortcuts import (  # noqa
        redirect, get_object_or_404)
from django.contrib import messages  # noqa
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.db import connection
from django import forms
from django.db import transaction
from django.utils.timezone import now

from courseflow.utils import StyledForm
from crispy_forms.layout import Submit

from course.utils import course_view, render_course_page
from course.models import (
        Participation, participation_role, participation_status,
        GradingOpportunity, GradeChange, GradeStateMachine,
        grade_state_change_types,
        FlowSession)


# {{{ student grade book

@course_view
def view_participant_grades(pctx, participation_id=None):
    if pctx.participation is None:
        raise PermissionDenied("must be enrolled to view grades")

    if participation_id is not None:
        grade_participation = Participation.objects.get(id=int(participation_id))
    else:
        grade_participation = pctx.participation

    if pctx.role in [
            participation_role.instructor,
            participation_role.teaching_assistant]:
        is_student_viewing = False
    elif pctx.role == participation_role.student:
        if grade_participation != pctx.participation:
            raise PermissionDenied("may not view other people's grades")

        is_student_viewing = True
    else:
        raise PermissionDenied()

    # NOTE: It's important that these two queries are sorted consistently,
    # also consistently with the code below.
    grading_opps = list((GradingOpportunity.objects
            .filter(
                course=pctx.course,
                shown_in_grade_book=True,
                )
            .order_by("identifier")))

    grade_changes = list(GradeChange.objects
            .filter(
                participation=grade_participation,
                opportunity__course=pctx.course,
                opportunity__shown_in_grade_book=True)
            .order_by(
                "participation__id",
                "opportunity__identifier",
                "grade_time")
            .prefetch_related("participation")
            .prefetch_related("participation__user")
            .prefetch_related("opportunity"))

    idx = 0

    grade_table = []
    for opp in grading_opps:
        if is_student_viewing:
            if not (opp.shown_in_grade_book
                    and opp.shown_in_student_grade_book):
                continue
        else:
            if not opp.shown_in_grade_book:
                continue

        while (
                idx < len(grade_changes)
                and grade_changes[idx].opportunity.identifier < opp.identifier
                ):
            idx += 1

        my_grade_changes = []
        while (
                idx < len(grade_changes)
                and grade_changes[idx].opportunity.pk == opp.pk):
            my_grade_changes.append(grade_changes[idx])
            idx += 1

        state_machine = GradeStateMachine()
        state_machine.consume(my_grade_changes)

        grade_table.append(
                GradeInfo(
                    opportunity=opp,
                    grade_state_machine=state_machine))

    return render_course_page(pctx, "course/gradebook-participant.html", {
        "grade_table": grade_table,
        "grade_participation": grade_participation,
        "grading_opportunities": grading_opps,
        "grade_state_change_types": grade_state_change_types,
        })

# }}}


# {{{ teacher grade book

class GradeInfo:
    def __init__(self, opportunity, grade_state_machine):
        self.opportunity = opportunity
        self.grade_state_machine = grade_state_machine


@course_view
def view_gradebook(pctx):
    if pctx.role not in [
            participation_role.instructor,
            participation_role.teaching_assistant]:
        raise PermissionDenied("must be instructor or TA to view grades")

    # NOTE: It's important that these three queries are sorted consistently,
    # also consistently with the code below.
    grading_opps = list((GradingOpportunity.objects
            .filter(
                course=pctx.course,
                shown_in_grade_book=True,
                )
            .order_by("identifier")))

    participations = list(Participation.objects
            .filter(
                course=pctx.course,
                status=participation_status.active)
            .order_by("id")
            .prefetch_related("user"))

    grade_changes = list(GradeChange.objects
            .filter(
                opportunity__course=pctx.course,
                opportunity__shown_in_grade_book=True)
            .order_by(
                "participation__id",
                "opportunity__identifier",
                "grade_time")
            .prefetch_related("participation")
            .prefetch_related("participation__user")
            .prefetch_related("opportunity"))

    idx = 0

    grade_table = []
    for participation in participations:
        while (
                idx < len(grade_changes)
                and grade_changes[idx].participation.id < participation.id):
            idx += 1

        grade_row = []
        for opp in grading_opps:
            while (
                    idx < len(grade_changes)
                    and grade_changes[idx].participation.pk == participation.pk
                    and grade_changes[idx].opportunity.identifier < opp.identifier
                    ):
                idx += 1

            my_grade_changes = []
            while (
                    idx < len(grade_changes)
                    and grade_changes[idx].opportunity.pk == opp.pk
                    and grade_changes[idx].participation.pk == participation.pk):
                my_grade_changes.append(grade_changes[idx])
                idx += 1

            state_machine = GradeStateMachine()
            state_machine.consume(my_grade_changes)

            grade_row.append(
                    GradeInfo(
                        opportunity=opp,
                        grade_state_machine=state_machine))

        grade_table.append(grade_row)

    grade_table = sorted(zip(participations, grade_table),
            key=lambda (participation, grades):
                (participation.user.last_name.lower(),
                    participation.user.first_name.lower()))

    return render_course_page(pctx, "course/gradebook.html", {
        "grade_table": grade_table,
        "grading_opportunities": grading_opps,
        "participations": participations,
        "grade_state_change_types": grade_state_change_types,
        })

# }}}


# {{{ grades by grading opportunity

class OpportunityGradeInfo(object):
    def __init__(self, grade_state_machine, flow_sessions):
        self.grade_state_machine = grade_state_machine
        self.flow_sessions = flow_sessions


class ModifySessionsForm(StyledForm):
    def __init__(self, rule_ids, *args, **kwargs):
        super(ModifySessionsForm, self).__init__(*args, **kwargs)

        self.fields["rule_id"] = forms.ChoiceField(
                choices=tuple(
                    (rule_id, str(rule_id))
                    for rule_id in rule_ids))

        self.helper.add_input(
                Submit("end", "End sessions and grade",
                    css_class="col-lg-offset-2"))
        self.helper.add_input(
                Submit("regrade", "Regrade ended sessions"))


@transaction.atomic
def finish_in_progress_sessions(repo, course, flow_id, rule_id):
    sessions = (FlowSession.objects
            .filter(
                course=course,
                flow_id=flow_id,
                access_rules_id=rule_id,
                in_progress=True,
                ))

    count = 0

    from course.flow import finish_flow_session_standalone
    for session in sessions:
        finish_flow_session_standalone(repo, course, session)
        count += 1

    return count


@transaction.atomic
def regrade_ended_sessions(repo, course, flow_id, rule_id):
    sessions = (FlowSession.objects
            .filter(
                course=course,
                flow_id=flow_id,
                access_rules_id=rule_id,
                in_progress=False,
                ))

    count = 0

    from course.flow import regrade_session
    for session in sessions:
        regrade_session(repo, course, session)
        count += 1

    return count


RULE_ID_NONE_STRING = "<<<NONE>>>"


def mangle_rule_id(rule_id):
    if rule_id is None:
        return RULE_ID_NONE_STRING
    else:
        return rule_id


@course_view
def view_grades_by_opportunity(pctx, opp_id):
    if pctx.role not in [
            participation_role.instructor,
            participation_role.teaching_assistant]:
        raise PermissionDenied("must be instructor or TA to view grades")

    opportunity = get_object_or_404(GradingOpportunity, id=int(opp_id))

    if pctx.course != opportunity.course:
        raise SuspiciousOperation("opportunity from wrong course")

    # {{{ end sessions form

    batch_session_ops_form = None
    if pctx.role == participation_role.instructor and opportunity.flow_id:
        cursor = connection.cursor()
        cursor.execute("select distinct access_rules_id from course_flowsession "
                "where course_id = %s and flow_id = %s "
                "order by access_rules_id", (pctx.course.id, opportunity.flow_id))
        rule_ids = [mangle_rule_id(row[0]) for row in cursor.fetchall()]

        request = pctx.request
        if request.method == "POST":
            batch_session_ops_form = ModifySessionsForm(
                    rule_ids, request.POST, request.FILES)
            if "end" in request.POST:
                op = "end"
            elif "regrade" in request.POST:
                op = "regrade"
            else:
                raise SuspiciousOperation("invalid operation")

            if batch_session_ops_form.is_valid():
                rule_id = batch_session_ops_form.cleaned_data["rule_id"]
                if rule_id == RULE_ID_NONE_STRING:
                    rule_id = None
                try:
                    if op == "end":
                        count = finish_in_progress_sessions(
                                pctx.repo, pctx.course, opportunity.flow_id,
                                rule_id)

                        messages.add_message(pctx.request, messages.SUCCESS,
                                "%d session(s) ended." % count)
                    elif op == "regrade":
                        count = regrade_ended_sessions(
                                pctx.repo, pctx.course, opportunity.flow_id,
                                rule_id)

                        messages.add_message(pctx.request, messages.SUCCESS,
                                "%d session(s) regraded." % count)
                    else:
                        raise SuspiciousOperation("invalid operation")
                except Exception as e:
                    messages.add_message(pctx.request, messages.ERROR,
                            "Error: %s %s" % (type(e).__name__, str(e)))

        else:
            batch_session_ops_form = ModifySessionsForm(rule_ids)

    # }}}

    # NOTE: It's important that these three queries are sorted consistently,
    # also consistently with the code below.

    participations = list(Participation.objects
            .filter(
                course=pctx.course,
                status=participation_status.active)
            .order_by("id")
            .prefetch_related("user"))

    grade_changes = list(GradeChange.objects
            .filter(opportunity=opportunity)
            .order_by(
                "participation__id",
                "grade_time")
            .prefetch_related("participation")
            .prefetch_related("participation__user")
            .prefetch_related("opportunity"))

    idx = 0

    grade_table = []
    for participation in participations:
        while (
                idx < len(grade_changes)
                and grade_changes[idx].participation.id < participation.id):
            idx += 1

        my_grade_changes = []
        while (
                idx < len(grade_changes)
                and grade_changes[idx].participation.pk == participation.pk):
            my_grade_changes.append(grade_changes[idx])
            idx += 1

        state_machine = GradeStateMachine()
        state_machine.consume(my_grade_changes)

        if opportunity.flow_id:
            flow_sessions = (FlowSession.objects
                    .filter(
                        participation=participation,
                        flow_id=opportunity.flow_id,
                        )
                    .order_by("start_time"))
        else:
            flow_sessions = None

        grade_table.append(
                OpportunityGradeInfo(
                    grade_state_machine=state_machine,
                    flow_sessions=flow_sessions))

    grade_table = sorted(zip(participations, grade_table),
            key=lambda (participation, grades):
                (participation.user.last_name.lower(),
                    participation.user.first_name.lower()))

    return render_course_page(pctx, "course/gradebook-by-opp.html", {
        "opportunity": opportunity,
        "participations": participations,
        "grade_state_change_types": grade_state_change_types,
        "grade_table": grade_table,
        "batch_session_ops_form": batch_session_ops_form,
        })

# }}}


# {{{ view single grade

@course_view
def view_single_grade(pctx, participation_id, opportunity_id):
    participation = get_object_or_404(Participation,
            id=int(participation_id))

    if participation.course != pctx.course:
        raise SuspiciousOperation("participation does not match course")

    opportunity = get_object_or_404(GradingOpportunity, id=int(opportunity_id))

    if pctx.role in [
            participation_role.instructor,
            participation_role.teaching_assistant]:
        if not opportunity.shown_in_grade_book:
            messages.add_message(pctx.request, messages.INFO,
                    "This grade is not shown in the grade book.")
        if not opportunity.shown_in_student_grade_book:
            messages.add_message(pctx.request, messages.INFO,
                    "This grade is not shown in the student grade book.")

    elif pctx.role == participation_role.student:
        if participation != pctx.participation:
            raise PermissionDenied("may not view other people's grades")
        if not (opportunity.shown_in_grade_book
                and opportunity.shown_in_student_grade_book):
            raise PermissionDenied("grade has not been released")
    else:
        raise PermissionDenied()

    # {{{ modify sessions buttons

    if pctx.role in [
            participation_role.instructor,
            participation_role.teaching_assistant]:
        allow_session_actions = True

        request = pctx.request
        if pctx.request.method == "POST":
            action_re = re.compile("^(end|reopen|regrade)_([0-9]+)$")
            for key in request.POST.keys():
                action_match = action_re.match(key)
                if action_match:
                    break

            if not action_match:
                raise SuspiciousOperation("unknown action")

            session = FlowSession.objects.get(id=int(action_match.group(2)))
            op = action_match.group(1)

            from course.flow import (
                    reopen_session,
                    regrade_session,
                    finish_flow_session_standalone)

            try:
                if op == "end":
                    finish_flow_session_standalone(
                            pctx.repo, pctx.course, session)
                    messages.add_message(pctx.request, messages.SUCCESS,
                            "Session ended.")

                elif op == "regrade":
                    regrade_session(
                            pctx.repo, pctx.course, session)
                    messages.add_message(pctx.request, messages.SUCCESS,
                            "Session regraded.")

                elif op == "reopen":
                    reopen_session(session)
                    messages.add_message(pctx.request, messages.SUCCESS,
                            "Session reopened.")

                else:
                    raise SuspiciousOperation("invalid session operation")

            except Exception as e:
                messages.add_message(pctx.request, messages.ERROR,
                        "Error: %s %s" % (type(e).__name__, str(e)))
    else:
        allow_session_actions = False

    # }}}

    grade_changes = list(GradeChange.objects
            .filter(
                opportunity=opportunity,
                participation=participation)
            .order_by("grade_time")
            .prefetch_related("participation")
            .prefetch_related("participation__user")
            .prefetch_related("creator")
            .prefetch_related("opportunity"))

    state_machine = GradeStateMachine()
    state_machine.consume(grade_changes, set_is_superseded=True)

    flow_grade_aggregation_strategy_text = None

    if opportunity.flow_id:
        flow_sessions = list(FlowSession.objects
                .filter(
                    participation=participation,
                    flow_id=opportunity.flow_id,
                    )
                .order_by("start_time"))

        # {{{ fish out grade rules

        from course.content import get_flow_desc

        flow_desc = get_flow_desc(
                pctx.repo, pctx.course, opportunity.flow_id,
                pctx.course_commit_sha)
        from course.utils import (
                get_flow_access_rules,
                get_relevant_rules)
        all_flow_rules = get_flow_access_rules(pctx.course,
                participation, opportunity.flow_id, flow_desc)

        relevant_flow_rules = get_relevant_rules(
                all_flow_rules, pctx.participation.role, now())

        if hasattr(flow_desc, "grade_aggregation_strategy"):
            from course.models import GRADE_AGGREGATION_STRATEGY_CHOICES
            flow_grade_aggregation_strategy_text = (
                    dict(GRADE_AGGREGATION_STRATEGY_CHOICES)
                    [flow_desc.grade_aggregation_strategy])

        # }}}

    else:
        flow_sessions = None
        relevant_flow_rules = None

    return render_course_page(pctx, "course/gradebook-single.html", {
        "opportunity": opportunity,
        "grade_participation": participation,
        "grade_state_change_types": grade_state_change_types,
        "grade_changes": grade_changes,
        "state_machine": state_machine,
        "flow_sessions": flow_sessions,
        "allow_session_actions": allow_session_actions,
        "show_page_grades": pctx.role in [
            participation_role.instructor,
            participation_role.teaching_assistant
            ],

        "flow_rules": relevant_flow_rules,
        "flow_grade_aggregation_strategy": flow_grade_aggregation_strategy_text,
        })

# }}}


# {{{ import grades

class ImportGradesForm(StyledForm):
    def __init__(self, course, *args, **kwargs):
        super(ImportGradesForm, self).__init__(*args, **kwargs)

        self.fields["grading_opportunity"] = forms.ModelChoiceField(
            queryset=(GradingOpportunity.objects
                .filter(course=course)
                .order_by("identifier")))

        self.fields["attempt_id"] = forms.CharField(
                initial="main",
                required=True)
        self.fields["file"] = forms.FileField()

        self.fields["format"] = forms.ChoiceField(
                choices=(
                    ("csvhead", "CSV with Header"),
                    ("csv", "CSV"),
                    ))

        self.fields["id_column"] = forms.IntegerField(
                help_text="1-based column index for the Email or NetID "
                "used to locate student record",
                min_value=1)
        self.fields["points_column"] = forms.IntegerField(
                help_text="1-based column index for the (numerical) grade",
                min_value=1)
        self.fields["feedback_column"] = forms.IntegerField(
                help_text="1-based column index for further (textual) feedback",
                min_value=1, required=False)
        self.fields["max_points"] = forms.DecimalField(
            initial=100)

        self.helper.add_input(
                Submit("preview", "Preview",
                    css_class="col-lg-offset-2"))
        self.helper.add_input(
                Submit("import", "Import"))


def find_participant_from_id(course, id_str):
    id_str = id_str.strip().lower()

    matches = (Participation.objects
            .filter(
                course=course,
                status=participation_status.active,
                user__email__istartswith=id_str)
            .prefetch_related("user"))

    surviving_matches = []
    for match in matches:
        if match.user.email.lower() == id_str:
            surviving_matches.append(match)
            continue

        email = match.user.email.lower()
        at_index = email.index("@")
        assert at_index > 0
        uid = email[:at_index]

        if uid == id_str:
            surviving_matches.append(match)
            continue

    if not surviving_matches:
        raise ValueError("no participant found for '%s'" % id_str)
    if len(surviving_matches) > 1:
        raise ValueError("more than one participant found for '%s'" % id_str)

    return surviving_matches[0]


def csv_to_grade_changes(course, grading_opportunity, attempt_id, file_contents,
        id_column, points_column, feedback_column, max_points,
        creator, grade_time, has_header):
    result = []

    import csv

    total_count = 0
    spamreader = csv.reader(file_contents)
    for row in spamreader:
        if has_header:
            has_header = False
            continue

        total_count += 1

        gchange = GradeChange()
        gchange.opportunity = grading_opportunity
        gchange.participation = find_participant_from_id(course, row[id_column-1])
        gchange.state = grade_state_change_types.graded
        gchange.attempt_id = attempt_id
        gchange.points = float(row[points_column-1])
        gchange.max_points = max_points
        if feedback_column is not None:
            gchange.comment = row[feedback_column-1]

        gchange.creator = creator
        gchange.grade_time = grade_time

        last_grades = (GradeChange.objects
                .filter(
                    opportunity=grading_opportunity,
                    participation=gchange.participation,
                    attempt_id=gchange.attempt_id)
                .order_by("-grade_time")[:1])

        if last_grades.count():
            last_grade, = last_grades

            if (last_grade.status == grade_state_change_types.graded
                    and last_grade.points == gchange.points
                    and last_grade.max_points == gchange.max_points
                    and last_grade.comment == gchange.comment):
                continue

            result.append(gchange)

    return total_count, result


@course_view
@transaction.atomic
def import_grades(pctx):
    if pctx.role != participation_role.instructor:
        raise PermissionDenied()

    form_text = ""
    request = pctx.request
    if request.method == "POST":
        form = ImportGradesForm(
                pctx.course, request.POST, request.FILES)

        is_import = "import" in request.POST
        if form.is_valid():
            try:
                total_count, grade_changes = csv_to_grade_changes(
                        course=pctx.course,
                        grading_opportunity=form.cleaned_data["grading_opportunity"],
                        attempt_id=form.cleaned_data["attempt_id"],
                        file_contents=request.FILES["file"],
                        id_column=form.cleaned_data["id_column"],
                        points_column=form.cleaned_data["points_column"],
                        feedback_column=form.cleaned_data["feedback_column"],
                        max_points=form.cleaned_data["max_points"],
                        creator=request.user,
                        grade_time=now(),
                        has_header=form.cleaned_data["format"] == "csvhead")
            except Exception as e:
                messages.add_message(pctx.request, messages.ERROR,
                        "Error: %s %s" % (type(e).__name__, str(e)))
            else:
                if total_count != len(grade_changes):
                    messages.add_message(pctx.request, messages.INFO,
                            "%d grades found, %d unchanged."
                            % (total_count, total_count - len(grade_changes)))

                if is_import:
                    GradeChange.objects.bulk_create(grade_changes)
                    messages.add_message(pctx.request, messages.SUCCESS,
                            "%d grades imported." % len(grade_changes))
                else:
                    from django.template.loader import render_to_string
                    form_text = render_to_string(
                            "course/grade-import-preview.html", {
                                "grade_changes": grade_changes,
                                })

    else:
        form = ImportGradesForm(pctx.course)

    return render_course_page(pctx, "course/generic-course-form.html", {
        "form_description": "Import Grade Data",
        "form": form,
        "form_text": form_text,
        })

# }}}

# vim: foldmethod=marker
