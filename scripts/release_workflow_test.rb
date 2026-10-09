# frozen_string_literal: true

require "minitest/autorun"
require "yaml"

class ReleaseWorkflowTest < Minitest::Test
  ROLLOUT = ".github/workflows/update-images.yml"

  def setup
    @workflow = YAML.load_file(".github/workflows/release.yml")
    @rollout = YAML.load_file(ROLLOUT)
  end

  # YAML 1.1 reads a workflow's `on` key as true.
  def triggers(workflow)
    workflow.fetch("on") { workflow.fetch(true) }
  end

  # The one job of the release that rolls the cluster out, by calling update-images.yml.
  def rollout_job
    names = @workflow.fetch("jobs").select { |_, job| job["uses"] == "./#{ROLLOUT}" }.keys

    assert_equal 1, names.size, "one job of the release calls #{ROLLOUT}"
    names.first
  end

  # Every job the named one waits for: its `needs`, and theirs.
  def waits_for(name)
    jobs = @workflow.fetch("jobs")
    found = []
    queue = Array(jobs.fetch(name).fetch("needs", []))
    until queue.empty?
      need = queue.shift
      next if found.include?(need)

      found << need
      queue.concat(Array(jobs.fetch(need).fetch("needs", [])))
    end
    found
  end

  def test_docker_images_wait_for_npm_package_publication
    images_needs = Array(@workflow.fetch("jobs").fetch("images").fetch("needs", []))

    assert_includes images_needs, "npm"
  end

  def test_docker_image_matrix_includes_browser_image
    image_entries = @workflow
      .fetch("jobs")
      .fetch("images")
      .fetch("strategy")
      .fetch("matrix")
      .fetch("include")

    assert_includes image_entries, {
      "dir" => "browser",
      "name" => "surogates-agent-browser",
    }
    assert_path_exists "images/browser/Dockerfile"
  end

  def test_npm_packages_publish_with_release_tag_version
    publish_step = @workflow
      .fetch("jobs")
      .fetch("npm")
      .fetch("steps")
      .find { |step| step["name"] == "Publish SDK packages" }

    assert_includes publish_step.fetch("run"), '--version="${GITHUB_REF_NAME#v}"'
  end

  def test_github_release_waits_for_all_release_tasks
    jobs = @workflow.fetch("jobs")
    release_job = jobs.fetch("release")
    release_needs = Array(release_job.fetch("needs", []))

    assert_includes release_needs, "wheel"
    assert_includes release_needs, "images"
    assert release_job.fetch("steps").any? { |step| step["uses"] == "softprops/action-gh-release@v2" }

    jobs.except("release").each_value do |job|
      refute job.fetch("steps", []).any? { |step| step["uses"] == "softprops/action-gh-release@v2" }
    end
  end

  def test_the_cluster_s_rollout_waits_for_no_job_of_the_desktop_s
    jobs = @workflow.fetch("jobs")
    # By this rule, so that a desktop job added later is held too.
    desktop = jobs.keys.select { |name| name.start_with?("desktop-") }

    refute_empty desktop
    assert_empty Array(jobs.fetch(rollout_job).fetch("needs", [])) & desktop
    # Nor through a job it needs: a desktop job that fails, or waits for its reviewer, would skip
    # or hold the rollout as surely from there.
    assert_empty waits_for(rollout_job) & desktop
  end

  def test_the_cluster_s_rollout_waits_for_the_whole_of_the_cloud_s_release
    # The nodes update to what the images job pushed. A tag whose npm packages, wheel or GitHub
    # release fails rolls nothing out, as when the rollout waited for the whole run.
    assert_equal %w[images npm release wheel], waits_for(rollout_job).sort
    # always() or !cancelled() would roll out a release that failed.
    refute @workflow.fetch("jobs").fetch(rollout_job).key?("if")
  end

  def test_the_cluster_s_rollout_holds_no_permission_on_the_repository
    # It holds the cluster's SSH key and reads nothing of the repository's: without this it would
    # take the workflow's contents: write and packages: write.
    assert_equal({}, @workflow.fetch("jobs").fetch(rollout_job)["permissions"])
    # A called workflow can narrow what its caller grants and never widen it: it asks for none.
    refute @rollout.key?("permissions")
    @rollout.fetch("jobs").each { |name, job| refute job.key?("permissions"), "#{name} asks for permissions of its own" }
  end

  def test_the_rollout_is_started_by_the_release_s_call_and_never_by_how_a_run_ended
    on = triggers(@rollout)

    refute on.key?("workflow_run"), "a failed desktop job fails the run, and the rollout would be skipped without a word"
    assert on.key?("workflow_call")
    # Called by the release, a job sees the tag's push as its event: a condition on the event
    # skips it there, and a skipped job is no failure.
    @rollout.fetch("jobs").each { |name, job| refute job.key?("if"), "#{name} has a condition of its own" }
  end

  def test_the_rollout_can_still_be_started_by_hand
    assert triggers(@rollout).key?("workflow_dispatch")
  end

  def test_the_rollout_is_handed_each_secret_it_reads_by_name_and_no_other
    jobs = @rollout.fetch("jobs").to_s
    read = jobs.scan(/\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)/).flatten.uniq.sort
    declared = triggers(@rollout).dig("workflow_call", "secrets") || {}
    handed = @workflow.fetch("jobs").fetch(rollout_job)["secrets"]

    assert_equal %w[MASTER_HOST NODE_HOSTS SSH_KEY], read
    # secrets['X'] or toJSON(secrets) would read a secret this test cannot name.
    refute_match(/\bsecrets\b(?!\.[A-Za-z_])/, jobs)
    # A called workflow reads only the secrets it declares and its caller hands it. Any other
    # comes empty, and the job does not always fail on it: with no NODE_HOSTS it updates no node,
    # restarts the pods and ends well.
    assert_equal read, declared.keys.sort
    assert_equal read.to_h { |name| [name, "${{ secrets.#{name} }}"] }, handed
  end

  def test_release_notes_are_generated_from_commit_messages
    steps = @workflow.fetch("jobs").fetch("release").fetch("steps")

    generate_step = steps.find { |step| step["name"] == "Generate release notes" }
    refute_nil generate_step, "Expected a release-notes generation step"
    assert_includes generate_step.fetch("run"), "scripts/release-notes.mjs"
    assert_path_exists "scripts/release-notes.mjs"

    release_step = steps.find { |step| step["uses"] == "softprops/action-gh-release@v2" }
    assert_equal "release-notes.md", release_step.fetch("with").fetch("body_path")
    refute release_step.fetch("with").key?("generate_release_notes")
  end

  def test_the_release_uploads_the_wheel_and_the_sdist_alone
    release_step = @workflow.fetch("jobs").fetch("release").fetch("steps").find { |step| step["uses"] == "softprops/action-gh-release@v2" }

    # Not whatever the build left in dist/: a desktop-vm-<key>.json there would be an anchor.
    assert_equal %w[dist/*.whl dist/*.tar.gz], release_step.fetch("with").fetch("files").split("\n")
  end

  def test_desktop_vm_image_is_published_once_per_key_after_the_kernel_check
    job = @workflow.fetch("jobs").fetch("desktop-vm-image")
    steps = job.fetch("steps")
    runs = steps.map { |step| step["run"].to_s }

    assert_equal({ "group" => "desktop-vm-image", "cancel-in-progress" => false }, job.fetch("concurrency"))
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal({ "state" => "${{ steps.published.outputs.state }}" }, job.fetch("outputs"))
    kernel = runs.index { |run| run.include?("images/guest/kernel-current.sh") }
    fetch = runs.index { |run| run.include?("images/guest/publish.sh fetch images/guest/out") }
    build = runs.index { |run| run.include?("images/guest/build.sh --packed images/guest/out") }
    send = runs.index { |run| run.include?("images/guest/publish.sh send images/guest/out") }
    refute_nil kernel
    assert_operator kernel, :<, fetch
    assert_operator fetch, :<, build
    assert_operator build, :<, send
    assert_equal "published", steps[fetch].fetch("id")
    [build, send].each do |index|
      assert_equal "steps.published.outputs.state == 'missing'", steps[index].fetch("if")
    end
    keep = steps.find { |step| step["uses"] == "actions/upload-artifact@v4" }
    assert_equal "images/guest/out/manifest.json", keep.fetch("with").fetch("path")
    %w[images/guest/inputs.sh images/guest/build.sh images/guest/publish.sh images/guest/kernel-current.sh].each do |script|
      assert File.executable?(script), "#{script} is not executable"
    end
  end

  def test_only_the_jobs_that_write_a_release_may_write_its_assets
    jobs = @workflow.fetch("jobs")

    # A release's desktop-vm-<key>.json anchors the desktop's image: wheel builds with unpinned
    # dependencies and images runs third-party actions, and neither uploads a release asset.
    assert_equal({ "contents" => "read" }, jobs.fetch("wheel").fetch("permissions"))
    assert_equal({ "contents" => "read", "packages" => "write" }, jobs.fetch("images").fetch("permissions"))
  end

  def test_desktop_vm_jobs_are_bounded_past_the_wait_for_a_key_s_manifest
    jobs = @workflow.fetch("jobs")

    # publish.sh fetch waits up to 15 times 60 s for the release that sent a key to attach its manifest.
    assert_operator jobs.fetch("desktop-vm-image").fetch("timeout-minutes"), :>, 15
    assert jobs.fetch("desktop-vm-manifest").key?("timeout-minutes")
  end

  def test_desktop_vm_image_gives_the_r2_secrets_to_its_two_publish_steps_alone
    job = @workflow.fetch("jobs").fetch("desktop-vm-image")
    r2 = {
      "S3_ENDPOINT" => "${{ secrets.R2_ENDPOINT }}",
      "S3_BUCKET" => "${{ secrets.R2_BUCKET }}",
      "AWS_ACCESS_KEY_ID" => "${{ secrets.R2_ACCESS_KEY_ID }}",
      "AWS_SECRET_ACCESS_KEY" => "${{ secrets.R2_SECRET_ACCESS_KEY }}",
    }

    refute job.key?("env"), "the job's env reaches every step"
    job.fetch("steps").each do |step|
      run = step["run"].to_s
      if run.include?("images/guest/publish.sh fetch")
        assert_equal r2.merge("GH_TOKEN" => "${{ github.token }}"), step.fetch("env")
      elsif run.include?("images/guest/publish.sh send")
        assert_equal r2, step.fetch("env")
      else
        refute step.to_s.include?("secrets."), "#{step["name"] || step["uses"]} reads a secret"
      end
    end
  end

  def test_desktop_vm_manifest_is_attached_to_every_release_that_ships_its_key
    job = @workflow.fetch("jobs").fetch("desktop-vm-manifest")
    run = job.fetch("steps").map { |step| step["run"].to_s }.join("\n")

    assert_equal %w[release desktop-vm-image], Array(job.fetch("needs"))
    # Published by this release or checked against an earlier one's: a release deleted strands no key.
    refute job.key?("if")
    assert_includes run, "gh release view"
    assert_equal({ "contents" => "write" }, job.fetch("permissions"))
    download = job.fetch("steps").find { |step| step["uses"] == "actions/download-artifact@v4" }
    assert_equal "desktop-vm-manifest", download.fetch("with").fetch("name")
    assert_includes run, 'desktop-vm-${key}.json'
    assert_includes run, 'gh release upload "$GITHUB_REF_NAME"'
  end

  def test_the_cloud_release_does_not_wait_for_the_desktop_image
    release_needs = Array(@workflow.fetch("jobs").fetch("release").fetch("needs", []))

    refute_includes release_needs, "desktop-vm-image"
    refute_includes release_needs, "desktop-vm-manifest"
  end
end
