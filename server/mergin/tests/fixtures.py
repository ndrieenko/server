# Copyright (C) Lutra Consulting Limited
#
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-MerginMaps-Commercial

import os
import sys
import uuid
from copy import deepcopy
from shutil import copy, move
from flask import current_app
from flask_login import current_user
from sqlalchemy.orm.attributes import flag_modified
from pygeodiff import GeoDiff
import pytest

from .. import db, create_app
from ..sync.models import Project, Upload, ProjectVersion, ProjectAccess
from ..sync.utils import generate_checksum, is_versioned_file, resolve_tags
from ..stats.app import register
from ..stats.models import MerginInfo
from . import test_project, test_workspace_id, test_project_dir, TMP_DIR
from .utils import login_as_admin, initialize, cleanup, file_info, create_project

thisdir = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(thisdir, os.pardir))


@pytest.fixture(scope="function")
def flask_app(request):
    """Flask app with fresh db and initialized empty tables"""
    from ..sync.db_events import remove_events

    application = create_app(
        ["SERVER_TYPE", "DOCS_URL", "COLLECT_STATISTICS", "USER_SELF_REGISTRATION"]
    )
    register(application)
    application.config["TEST_DIR"] = os.path.join(thisdir, "test_projects")
    application.config["SERVER_NAME"] = "localhost.localdomain"
    application.config["SERVER_TYPE"] = "ce"
    application.config["SERVICE_ID"] = str(uuid.uuid4())
    app_context = application.app_context()
    app_context.push()

    with app_context:
        db.create_all()

    def teardown():
        # clean up db
        db.session.remove()
        db.drop_all()
        db.engine.dispose()

        app_context.pop()
        # detach db hooks
        remove_events()

    request.addfinalizer(teardown)
    return application


@pytest.fixture(scope="function")
def app(flask_app, request):
    """Flask app with testing objects created"""
    with flask_app.app_context():
        initialize()
        info = MerginInfo(service_id=current_app.config["SERVICE_ID"])
        db.session.add(info)
        db.session.commit()

    def teardown():
        # remove all project files
        with flask_app.app_context():
            dirs = [p.storage.project_dir for p in Project.query.all()]
            cleanup(flask_app.test_client(), dirs)

    request.addfinalizer(teardown)
    return flask_app


@pytest.fixture(scope="function")
def client(app):
    """Flask app tests client with already logged-in superuser"""
    client = app.test_client()
    login_as_admin(client)
    return client


@pytest.fixture(scope="function")
def diff_project(app):
    """Modify testing project to contain some history with diffs. Geodiff lib is used to handle changes.
    Files are copied to location where server would expect it. Corresponding changes metadata and project versions
    are created and stored in db.

    Following changes are applied to base.gpkg in tests project (v1):
    v2: removed file -> previous version is lost (unless requested explicitly)
    v3: uploaded again
    v4: patched with changes from inserted_1_A.gpkg (1 inserted feature)
    v5: replaced with original file base.gpkg (mimic of force update)
    v6: patched with changes from modified_1_geom.gpkg (translated feature)
    v7: patched with changes from inserted_1_B.gpkg (1 inserted feature), final state is modified_1_geom.gpkg + inserted_1_B.gpkg
    v8: nothing happened, just to ensure last diff is not last version of project file
    v9: renamed to test.gpkg base.gpkg has been removed removed and tests.gpkg has been added
    v10: nothing happened (although officially forbidden here it mimics no changes to file of interest)
    """
    from .test_project_controller import create_diff_meta

    test_gpkg_file = os.path.join(test_project_dir, "test.gpkg")
    try:
        geodiff = GeoDiff()
        project = Project.query.filter_by(
            name=test_project, workspace_id=test_workspace_id
        ).first()

        update_meta = file_info(test_project_dir, "base.gpkg")
        diff_meta_A = create_diff_meta(
            "base.gpkg", "inserted_1_A.gpkg", test_project_dir
        )
        diff_meta_mod = create_diff_meta(
            "base.gpkg", "modified_1_geom.gpkg", test_project_dir
        )

        patch = os.path.join(TMP_DIR, "patch")

        basefile = os.path.join(test_project_dir, "base.gpkg")
        copy(basefile, patch)
        copy(basefile, test_gpkg_file)
        geodiff.apply_changeset(
            patch, os.path.join(TMP_DIR, diff_meta_mod["diff"]["path"])
        )
        diff_meta_B = create_diff_meta(
            "base.gpkg", "inserted_1_B.gpkg", test_project_dir
        )

        changes = [
            {
                "added": [],
                "removed": [file_info(test_project_dir, "base.gpkg")],
                "updated": [],
            },
            {
                "added": [file_info(test_project_dir, "base.gpkg")],
                "removed": [],
                "updated": [],
            },
            {"added": [], "removed": [], "updated": [diff_meta_A]},
            {
                "added": [],
                "removed": [],
                "updated": [update_meta],
            },  # force update with full file
            {"added": [], "removed": [], "updated": [diff_meta_mod]},
            {"added": [], "removed": [], "updated": [diff_meta_B]},
            {
                "added": [],
                "removed": [],
                "updated": [],
            },  # final state of base.gpkg (v8)
            {
                "added": [file_info(test_project_dir, "test.gpkg")],
                "removed": [file_info(test_project_dir, "base.gpkg")],
                "updated": [],
            },  # file renamed, by removing old and upload new - break of history
            {"added": [], "removed": [], "updated": []},
        ]
        version_files = project.files
        for i, change in enumerate(changes):
            ver = "v{}".format(i + 2)
            if change["added"]:
                meta = deepcopy(
                    change["added"][0]
                )  # during push we do not store 'location' in 'added' metadata
                meta["location"] = os.path.join(ver, meta["path"])
                new_file = os.path.join(project.storage.project_dir, meta["location"])
                os.makedirs(os.path.dirname(new_file), exist_ok=True)
                copy(os.path.join(test_project_dir, meta["path"]), new_file)
                version_files.append(meta)
            elif change["updated"]:
                meta = change["updated"][0]
                f_updated = next(f for f in version_files if f["path"] == meta["path"])
                new_location = os.path.join(ver, f_updated["path"])
                patchedfile = os.path.join(project.storage.project_dir, new_location)
                os.makedirs(os.path.dirname(patchedfile), exist_ok=True)
                if "diff" in meta.keys():
                    basefile = os.path.join(
                        project.storage.project_dir, f_updated["location"]
                    )
                    changefile = os.path.join(TMP_DIR, meta["diff"]["path"])
                    copy(basefile, patchedfile)
                    geodiff.apply_changeset(patchedfile, changefile)
                    meta["diff"]["location"] = os.path.join(ver, meta["diff"]["path"])
                    move(
                        changefile,
                        os.path.join(
                            project.storage.project_dir, meta["diff"]["location"]
                        ),
                    )
                else:
                    copy(os.path.join(test_project_dir, f_updated["path"]), patchedfile)
                    f_updated.pop("diff", None)
                meta["location"] = new_location
                f_updated.update(meta)
            if change["removed"]:
                f_removed = next(
                    f
                    for f in version_files
                    if f["path"] == change["removed"][0]["path"]
                )
                version_files.remove(f_removed)
            else:
                pass

            pv = ProjectVersion(
                project,
                ver,
                project.creator.username,
                change,
                version_files,
                "127.0.0.1",
            )
            db.session.add(pv)
            db.session.commit()
            version_files = pv.files
            current_version = pv.name
            assert pv.project_size == sum(file["size"] for file in version_files)

        project.files = version_files
        project.disk_usage = sum(file["size"] for file in project.files)
        project.tags = resolve_tags(version_files)
        project.latest_version = current_version
        db.session.add(project)
        flag_modified(project, "files")
        db.session.commit()
    finally:
        os.remove(test_gpkg_file)
    return project
