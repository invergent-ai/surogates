# frozen_string_literal: true

require "minitest/autorun"
require "yaml"

class ReleaseWorkflowTest < Minitest::Test
  def setup
    @workflow = YAML.load_file(".github/workflows/release.yml")
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
