# frozen_string_literal: true

require "minitest/autorun"
require "yaml"

class ReleaseWorkflowTest < Minitest::Test
  def setup
    @workflow = YAML.load_file(".github/workflows/release.yml")
  end

  def test_the_release_runs_for_a_pushed_version_tag_and_for_nothing_else
    # Ruby's YAML reads the key "on" as true. A pull request, a fork's among them, a push of a
    # branch and a run started by hand run none of the release's jobs: neither the one that holds
    # the desktop's release key, nor those that write to R2, GHCR or npm.
    refute @workflow.key?("on")
    assert_equal({ "push" => { "tags" => ["v*"] } }, @workflow.fetch(true))
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

  def test_every_job_needs_only_jobs_of_the_workflow
    jobs = @workflow.fetch("jobs")

    jobs.each do |name, job|
      Array(job.fetch("needs", [])).each { |need| assert jobs.key?(need), "#{name} needs #{need}, which is no job of the release" }
    end
  end

  def test_desktop_build_makes_the_tarball_from_the_tag_with_the_vm_image_and_keeps_it
    job = @workflow.fetch("jobs").fetch("desktop-build")
    steps = job.fetch("steps")
    runs = steps.map { |step| step["run"].to_s }

    assert_equal ["desktop-vm-image"], Array(job.fetch("needs"))
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    build = steps.find { |step| step["name"] == "Build the app" }
    assert_equal "desktop", build.fetch("working-directory")
    assert_equal "/dev/null", build.fetch("env").fetch("NPM_CONFIG_USERCONFIG")
    commands = build.fetch("run").lines.map(&:strip).reject { |line| line.empty? || line.start_with?("#") }
    assert_equal ["rm -rf bin", "npm ci", "node node_modules/electron/install.js", "npm run build"], commands
    vm = steps.index { |step| step["uses"] == "actions/download-artifact@v4" }
    assert_equal "desktop-vm-manifest", steps[vm].fetch("with").fetch("name")
    # The job packages once, and its command is this, whole: a fourth argument would name the
    # install script packed as the root helper, which only a test names.
    packagings = runs.each_index.select { |index| runs[index].include?("package.sh") }
    assert_equal ['desktop/scripts/package.sh "${GITHUB_REF_NAME#v}" out/vm/manifest.json out/desktop'], packagings.map { |index| runs[index].strip }
    package = packagings.first
    keep = steps.index { |step| step["uses"] == "actions/upload-artifact@v4" }
    refute_nil package
    assert_operator vm, :<, package
    assert_operator package, :<, keep
    # The agent's disk is made under fakeroot, which the job installs before it packages.
    fakeroot = runs.index { |run| run.match?(/\bapt-get install\b.*\bfakeroot\b/) }
    refute_nil fakeroot
    assert_operator fakeroot, :<, package
    assert_equal "desktop-tarball", steps[keep].fetch("with").fetch("name")
    assert_equal "out/desktop/surogate-desktop-*-linux-x64.tar.gz", steps[keep].fetch("with").fetch("path")
    # The tarball's hash and its size are the job's own outputs, which no other job of the run can
    # set: an artifact is the run's, and any of its jobs may put another file under the tarball's
    # name. The signing opens no tarball, and has these two words for what it signs.
    assert_equal({ "sha256" => "${{ steps.tarball.outputs.sha256 }}", "size" => "${{ steps.tarball.outputs.size }}" }, job.fetch("outputs"))
    hash = steps.index { |step| step["id"] == "tarball" }
    refute_nil hash
    assert_operator package, :<, hash
    assert_equal [
      'echo "sha256=$(sha256sum <"out/desktop/surogate-desktop-${GITHUB_REF_NAME#v}-linux-x64.tar.gz" | cut -d\' \' -f1)" >>"$GITHUB_OUTPUT"',
      'echo "size=$(stat -c %s "out/desktop/surogate-desktop-${GITHUB_REF_NAME#v}-linux-x64.tar.gz")" >>"$GITHUB_OUTPUT"',
    ], steps[hash].fetch("run").lines.map(&:strip)
  end

  def test_desktop_describe_writes_the_manifest_of_the_built_tarball_in_a_job_that_holds_no_secret
    job = @workflow.fetch("jobs").fetch("desktop-describe")
    steps = job.fetch("steps")

    # All that reads the build's tarball, on a runner of its own: a step of the job that signs
    # could write into that job's checkout, or leave something running beside its key. The job
    # is these four steps and nothing else, each action by its commit (v4.4.0, v4.3.0, v4.6.2).
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" },
      { "run" => 'desktop/release/publish.sh describe "${GITHUB_REF_NAME#v}" out/desktop' },
      { "uses" => "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" },
    ], steps.map { |step| step.slice("uses", "run") }
    assert_equal [%w[uses], %w[name uses with], %w[env name run], %w[name uses with]], steps.map { |step| step.keys.sort }
    assert_equal({ "name" => "desktop-tarball", "path" => "out/desktop" }, steps[1].fetch("with"))
    # The hash the build's job gave for its tarball, through the step's environment, never pasted
    # into its script, where what the build says would be run.
    assert_equal({ "DESKTOP_TARBALL_SHA256" => "${{ needs.desktop-build.outputs.sha256 }}" }, steps[2].fetch("env"))
    # The manifest alone is handed on, and a run that wrote none hands on nothing.
    assert_equal({ "name" => "desktop-manifest", "path" => "out/desktop/manifest.json", "if-no-files-found" => "error" }, steps[3].fetch("with"))
    # No secret by any name, no Environment, which would hand it every secret of that one, and no
    # env of the job's own; a runner of its own; and the build before it, whose hash it checks.
    assert_equal %w[needs permissions runs-on steps timeout-minutes], job.keys.sort
    assert_equal ["desktop-build"], Array(job.fetch("needs"))
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
    refute_match(/\bsecrets\b/, job.to_s)
    refute_includes job.to_s, "DESKTOP_RELEASE_KEY"
    refute_includes job.to_s, "desktop-release"
    # Nothing of the dependency tree: no npm, no node, and no script of the package's but the one.
    steps.each do |step|
      refute_match(/\b(npm|npx|node|package\.sh)\b/, step["run"].to_s, "#{step["name"] || step["uses"]} runs npm, node or the packaging")
      refute_includes step["uses"].to_s, "setup-node"
    end
  end

  def test_desktop_publish_signs_and_sends_the_built_tarball_one_release_at_a_time_in_its_environment
    job = @workflow.fetch("jobs").fetch("desktop-publish")
    steps = job.fetch("steps")
    runs = steps.map { |step| step["run"].to_s }

    # Both: the build, for its two words of the tarball, and the job that wrote the manifest, which
    # this one does not write itself.
    assert_equal %w[desktop-build desktop-describe], Array(job.fetch("needs"))
    assert_equal "desktop-release", job.fetch("environment")
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal({ "group" => "desktop-release", "cancel-in-progress" => false }, job.fetch("concurrency"))
    taken = steps.each_index.select { |index| steps[index]["uses"].to_s.start_with?("actions/download-artifact@") }
    assert_equal [{ "name" => "desktop-tarball", "path" => "out/desktop" }, { "name" => "desktop-manifest", "path" => "out/desktop" }], taken.map { |index| steps[index].fetch("with") }
    sign = runs.index { |run| run.include?('desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop') }
    send = runs.index { |run| run.include?('desktop/release/publish.sh send "${GITHUB_REF_NAME#v}" out/desktop') }
    refute_nil sign
    refute_nil send
    # The manifest and the tarball are here before the one is signed, and it is signed before both are sent.
    assert_operator taken.max, :<, sign
    assert_operator sign, :<, send
    # The job that holds the key reads no tarball: it writes no manifest of one, and unpacks none.
    runs.each do |run|
      refute_match(/\bdescribe\b/, run, "the job that holds the release key writes a tarball's manifest")
      refute_match(/\b(tar|unzip|gzip|gunzip|zcat)\b/, run, "the job that holds the release key unpacks an archive")
    end
    %w[desktop/scripts/package.sh desktop/release/publish.sh desktop/release/install.sh].each do |script|
      assert File.executable?(script), "#{script} is not executable"
    end
  end

  def test_only_the_desktop_s_publish_job_holds_its_secrets_and_it_runs_no_npm
    jobs = @workflow.fetch("jobs")
    r2 = {
      "S3_ENDPOINT" => "${{ secrets.R2_ENDPOINT }}",
      "S3_BUCKET" => "${{ secrets.R2_BUCKET }}",
      "AWS_ACCESS_KEY_ID" => "${{ secrets.R2_ACCESS_KEY_ID }}",
      "AWS_SECRET_ACCESS_KEY" => "${{ secrets.R2_SECRET_ACCESS_KEY }}",
    }

    # npm ci runs the dependency tree's install scripts, and a job's steps share its runner: the
    # build holds no secret, and the job that holds them installs and runs nothing of npm's.
    %w[desktop-build desktop-describe desktop-publish].each { |name| refute jobs.fetch(name).key?("env"), "#{name}'s env reaches every step" }
    %w[desktop-build desktop-describe].each do |name|
      jobs.fetch(name).fetch("steps").each do |step|
        refute step.to_s.include?("secrets."), "#{name}'s #{step["name"] || step["uses"]} reads a secret"
      end
    end
    jobs.fetch("desktop-publish").fetch("steps").each do |step|
      run = step["run"].to_s
      refute_match(/\b(npm|npx|node)\b/, run, "#{step["name"] || step["uses"]} runs npm or node")
      refute_includes step["uses"].to_s, "setup-node"
      if run.include?("publish.sh sign")
        # With the key, the build's own words for its tarball, its hash and its size: the signing
        # opens no tarball.
        assert_equal({
          "DESKTOP_RELEASE_KEY" => "${{ secrets.DESKTOP_RELEASE_KEY }}",
          "DESKTOP_TARBALL_SHA256" => "${{ needs.desktop-build.outputs.sha256 }}",
          "DESKTOP_TARBALL_SIZE" => "${{ needs.desktop-build.outputs.size }}",
        }, step.fetch("env"))
      elsif run.include?("publish.sh send")
        assert_equal r2, step.fetch("env")
      else
        refute step.to_s.include?("secrets."), "#{step["name"] || step["uses"]} reads a secret"
      end
    end
    jobs.except("desktop-publish").each { |name, job| refute job.to_s.include?("DESKTOP_RELEASE_KEY"), "#{name} reads the release key" }
    # The release key is in one step's environment, the signing's, and nowhere wider: not the
    # job's, which the check above refuses, and not the workflow's.
    keyed = jobs.fetch("desktop-publish").fetch("steps").select { |step| step.to_s.include?("DESKTOP_RELEASE_KEY") }
    assert_equal ['desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop'], keyed.map { |step| step["run"].to_s.strip }
    refute @workflow.except("jobs").to_s.include?("secrets."), "the workflow gives a secret to every job"
  end

  def test_no_workflow_turns_off_its_runner_s_restriction_of_user_namespaces
    # A stock Ubuntu 24.04 refuses a user namespace to a program without a profile of its own. No
    # job needs one, and none changes a kernel setting of its runner's to get one.
    workflows = Dir[".github/workflows/*.yml"]
    assert_includes workflows, ".github/workflows/release.yml"
    workflows.each do |workflow|
      refute_match(/apparmor_restrict_unprivileged_userns|\bsysctl\b|\bunshare\b/, File.read(workflow), workflow)
    end
  end

  def test_desktop_jobs_are_bounded
    jobs = @workflow.fetch("jobs")

    assert jobs.fetch("desktop-build").key?("timeout-minutes")
    assert jobs.fetch("desktop-describe").key?("timeout-minutes")
    assert jobs.fetch("desktop-publish").key?("timeout-minutes")
  end

  def test_the_cloud_release_does_not_wait_for_the_desktop
    release_needs = Array(@workflow.fetch("jobs").fetch("release").fetch("needs", []))

    refute_includes release_needs, "desktop-build"
    refute_includes release_needs, "desktop-describe"
    refute_includes release_needs, "desktop-publish"
  end

  def test_desktop_publish_is_the_tag_s_checkout_the_two_downloads_and_publish_sh_and_nothing_else
    job = @workflow.fetch("jobs").fetch("desktop-publish")
    steps = job.fetch("steps")

    # A step need not name npm to run it: package.sh does, and so may an action, a container's
    # image, or a runner that kept what an earlier job's npm left on it. The job that holds the
    # keys is these five steps, each with these keys alone, on a runner of its own.
    # Its two actions are named by their commits (v4.4.0 and v4.3.0): a tag can be moved to other
    # code, and the job that holds the release key would run it.
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" },
      { "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" },
      { "run" => 'desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop' },
      { "run" => 'desktop/release/publish.sh send "${GITHUB_REF_NAME#v}" out/desktop' },
    ], steps.map { |step| step.slice("uses", "run") }
    # The checkout names no ref, repository or path: publish.sh and install.sh are the tag's.
    assert_equal [%w[uses], %w[name uses with], %w[name uses with], %w[env name run], %w[env name run]], steps.map { |step| step.keys.sort }
    assert_equal %w[concurrency environment needs permissions runs-on steps timeout-minutes], job.keys.sort
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
  end

  def test_the_desktop_s_secrets_reach_no_other_job_however_they_are_named
    jobs = @workflow.fetch("jobs")

    # An Environment's secrets reach every job that names it, all of them at once through
    # toJSON(secrets), which names none.
    jobs.except("desktop-publish").each do |name, job|
      refute_includes job["environment"].to_s, "desktop-release", "#{name} runs in the desktop's Environment"
    end
    # secrets.X, secrets['X'] or all of them: the build names no secret in any form.
    refute_match(/\bsecrets\b/, jobs.fetch("desktop-build").to_s)
    # R2's keys can rewrite desktop/install.sh, which runs as root on each new install: the guest
    # image's job and the desktop's publish job read them, and no other.
    r2 = /\bR2_(ENDPOINT|BUCKET|ACCESS_KEY_ID|SECRET_ACCESS_KEY)\b/
    assert_equal %w[desktop-vm-image desktop-publish], jobs.select { |_, job| job.to_s.match?(r2) }.keys
  end
end
