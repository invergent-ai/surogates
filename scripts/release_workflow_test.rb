# frozen_string_literal: true

require "digest"
require "json"
require "minitest/autorun"
require "yaml"

class ReleaseWorkflowTest < Minitest::Test
  ROLLOUT = ".github/workflows/update-images.yml"
  MACOS = ".github/workflows/desktop-macos.yml"
  WINDOWS = ".github/workflows/desktop-windows.yml"
  ARM64 = ".github/workflows/desktop-guest-arm64.yml"

  def setup
    @workflow = YAML.load_file(".github/workflows/release.yml")
    @rollout = YAML.load_file(ROLLOUT)
  end

  # YAML 1.1 reads a workflow's `on` key as true.
  def triggers(workflow)
    workflow.fetch("on") { workflow.fetch(true) }
  end

  # The release's two jobs that roll the cluster out, by calling update-images.yml, by their conditions: the one while
  # Windows publishes nothing, and the one once the repository names its signing account. Only one of the two runs.
  ROLLOUTS = { "rollout" => "needs.desktop-signing-mode.outputs.signing == 'trial'", "rollout-with-windows" => "needs.desktop-signing-mode.outputs.signing == 'account'" }.freeze

  def rollout_jobs
    names = @workflow.fetch("jobs").select { |_, job| job["uses"] == "./#{ROLLOUT}" }.keys

    assert_equal ROLLOUTS.keys, names, "the release's jobs that call #{ROLLOUT}"
    names
  end

  # The Windows release's jobs: what the release is without them while Windows publishes nothing.
  def windows_jobs
    @workflow.fetch("jobs").keys.grep(/\Adesktop-win-/)
  end

  # Every job the named one waits for: its `needs`, and theirs.
  def waits_for(name)
    jobs = @workflow.fetch("jobs")
    found = []
    # A copy: Array() hands back the job's own list, which the walk below empties.
    queue = Array(jobs.fetch(name).fetch("needs", [])).dup
    until queue.empty?
      need = queue.shift
      next if found.include?(need)

      found << need
      queue.concat(Array(jobs.fetch(need).fetch("needs", [])))
    end
    found
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
    assert release_job.fetch("steps").any? { |step| step["uses"].to_s.start_with?("softprops/action-gh-release@") }

    jobs.except("release").each_value do |job|
      refute job.fetch("steps", []).any? { |step| step["uses"].to_s.start_with?("softprops/action-gh-release@") }
    end
  end

  def test_the_cluster_s_rollout_waits_for_every_other_job_of_the_release_and_for_windows_s_only_once_windows_publishes
    jobs = @workflow.fetch("jobs")
    # The nodes update to what the images job pushed, once the whole release has succeeded, the
    # desktop's jobs too: a job its needs do not reach could fail, and the cluster would be rolled
    # out all the same. By this rule, so that a job added later is waited for too.
    # While Windows publishes nothing (the repository names no signing account), the release is every job but Windows's,
    # and its rollout waits for no Windows job: a trial of Windows's that fails stops nothing of Linux's.
    unset = rollout_jobs.first
    assert_equal (jobs.keys - rollout_jobs - windows_jobs).sort, waits_for(unset).sort
    # Once it names one, Windows's publish is the release's too: its rollout waits for every other job, Windows's among them.
    set = rollout_jobs.last
    assert_equal (jobs.keys - rollout_jobs).sort, waits_for(set).sort
    assert_includes Array(jobs.fetch(set).fetch("needs")), "desktop-win-publish"
    # Each runs only where every need succeeded: a condition with no status check of its own is success() and it, and
    # always(), !cancelled() or a result read by hand would roll out a release that failed. And never both.
    ROLLOUTS.each do |name, condition|
      assert_equal condition, jobs.fetch(name).fetch("if"), name
      refute_match(/always|cancelled|failure|success|result/, jobs.fetch(name).fetch("if"), name)
    end
    # And no job of Linux's, nor of the cloud's release, waits for a Windows job, whatever the variable says.
    (jobs.keys - rollout_jobs - windows_jobs).each do |name|
      assert_empty waits_for(name) & windows_jobs, "#{name} waits for a Windows job"
    end
    # Windows's publish runs only once the account is named, and every Windows job is reached by it.
    assert_equal "#{MODE} == 'account'", jobs.fetch("desktop-win-publish").fetch("if")
    assert_equal (windows_jobs - ["desktop-win-publish"]).sort, (waits_for("desktop-win-publish") & windows_jobs).sort
  end

  def test_the_cluster_s_rollout_holds_no_permission_on_the_repository
    # It holds the cluster's SSH key and reads nothing of the repository's: without this it would
    # take the workflow's contents: write and packages: write.
    rollout_jobs.each { |name| assert_equal({}, @workflow.fetch("jobs").fetch(name)["permissions"], name) }
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

    assert_equal %w[MASTER_HOST NODE_HOSTS SSH_KEY], read
    # secrets['X'] or toJSON(secrets) would read a secret this test cannot name.
    refute_match(/\bsecrets\b(?!\.[A-Za-z_])/, jobs)
    # A called workflow reads only the secrets it declares and its caller hands it. Any other
    # comes empty, and the job does not always fail on it: with no NODE_HOSTS it updates no node,
    # restarts the pods and ends well.
    assert_equal read, declared.keys.sort
    rollout_jobs.each { |name| assert_equal read.to_h { |secret| [secret, "${{ secrets.#{secret} }}"] }, @workflow.fetch("jobs").fetch(name)["secrets"], name }
  end

  def test_release_notes_are_generated_from_commit_messages
    steps = @workflow.fetch("jobs").fetch("release").fetch("steps")

    generate_step = steps.find { |step| step["name"] == "Generate release notes" }
    refute_nil generate_step, "Expected a release-notes generation step"
    assert_includes generate_step.fetch("run"), "scripts/release-notes.mjs"
    assert_path_exists "scripts/release-notes.mjs"

    release_step = steps.find { |step| step["uses"].to_s.start_with?("softprops/action-gh-release@") }
    assert_equal "release-notes.md", release_step.fetch("with").fetch("body_path")
    refute release_step.fetch("with").key?("generate_release_notes")
  end

  def test_the_release_uploads_the_wheel_and_the_sdist_alone
    release_step = @workflow.fetch("jobs").fetch("release").fetch("steps").find { |step| step["uses"].to_s.start_with?("softprops/action-gh-release@") }

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
    kernel = runs.index { |run| run.include?("images/guest/kernel-current.sh --arch amd64") }
    fetch = runs.index { |run| run.include?("images/guest/publish.sh --arch amd64 fetch images/guest/out") }
    build = runs.index { |run| run.include?("images/guest/build.sh --arch amd64 --packed images/guest/out") }
    send = runs.index { |run| run.include?("images/guest/publish.sh --arch amd64 send images/guest/out") }
    refute_nil kernel
    assert_operator kernel, :<, fetch
    assert_operator fetch, :<, build
    assert_operator build, :<, send
    assert_equal "published", steps[fetch].fetch("id")
    [build, send].each do |index|
      assert_equal "steps.published.outputs.state == 'missing'", steps[index].fetch("if")
    end
    keep = steps.find { |step| step["uses"].to_s.start_with?("actions/upload-artifact@") }
    assert_equal "images/guest/out/manifest.json", keep.fetch("with").fetch("path")
    %w[images/guest/inputs.sh images/guest/build.sh images/guest/publish.sh images/guest/kernel-current.sh].each do |script|
      assert File.executable?(script), "#{script} is not executable"
    end
  end

  def test_the_release_builds_the_linux_app_s_guest_alone
    job = @workflow.fetch("jobs").fetch("desktop-vm-image")

    # amd64's, the Linux app's, on an amd64 runner, and each of the guest's scripts told so. A job
    # that needs a matrix waits for every leg of it: an arm64 leg that failed, or that no runner
    # took, would hold the Linux app's release and the attach of amd64's manifest. arm64's image is
    # built by hand (desktop-guest-arm64.yml) until the release ships the macOS app.
    refute job.key?("strategy")
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
    guest = job.fetch("steps").flat_map { |step| step["run"].to_s.scan(%r{images/guest/\S+\.sh[^\n]*}) }
    assert_equal 4, guest.size
    guest.each { |run| assert_match(/ --arch amd64\b/, run) }
    refute_match(/arm64/, @workflow.to_s)
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
      if run.include?("images/guest/publish.sh --arch amd64 fetch")
        assert_equal r2.merge("GH_TOKEN" => "${{ github.token }}"), step.fetch("env")
      elsif run.include?("images/guest/publish.sh --arch amd64 send")
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
    download = job.fetch("steps").find { |step| step["uses"].to_s.start_with?("actions/download-artifact@") }
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
    tarball = 'out/desktop/surogate-desktop-${GITHUB_REF_NAME#v}-linux-x64.tar.gz'

    assert_equal ["desktop-vm-image"], Array(job.fetch("needs"))
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal %w[needs outputs permissions runs-on steps timeout-minutes], job.keys.sort
    # What this job writes is what is signed, so the job is these steps, whole, and nothing between
    # them: the tag's own checkout, with no other ref named; the app built from it; the VM image's
    # manifest; the one packaging, with no fourth argument, which would name the install script
    # packed as the root helper and which only a test names; the tarball's hash and size, said
    # right after it is made; the tarball kept; and last the agent disk package.sh leaves beside it,
    # for the Windows app's build, which has no mke2fs. Each action by its commit.
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "name" => "Set up Node", "uses" => "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020", "with" => { "node-version" => "22" } },
      {
        "name" => "Build the app", "working-directory" => "desktop", "env" => { "NPM_CONFIG_USERCONFIG" => "/dev/null" },
        "run" => "# The app's node and its spelling dictionary are fetched and checked against their pins, never ones a runner kept.\nrm -rf bin dictionaries\nnpm ci\nnode node_modules/electron/install.js\nnpm run build\n",
      },
      {
        "name" => "Take the VM image's manifest", "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
        "with" => { "name" => "desktop-vm-manifest", "path" => "out/vm" },
      },
      # The agent's disk is made under fakeroot, which the job installs before it packages.
      { "name" => "Install fakeroot, which the agent's disk is made with", "run" => "sudo apt-get update -qq && sudo apt-get install -y -qq --no-install-recommends fakeroot" },
      { "name" => "Package the app", "run" => 'desktop/scripts/package.sh "${GITHUB_REF_NAME#v}" out/vm/manifest.json out/desktop' },
      {
        "name" => "Say the tarball's hash and size to the publish job", "id" => "tarball",
        "run" => "echo \"sha256=$(sha256sum <\"#{tarball}\" | cut -d' ' -f1)\" >>\"$GITHUB_OUTPUT\"\necho \"size=$(stat -c %s \"#{tarball}\")\" >>\"$GITHUB_OUTPUT\"\n",
      },
      {
        "name" => "Keep the tarball for the publish job", "uses" => "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "with" => { "name" => "desktop-tarball", "path" => "out/desktop/surogate-desktop-*-linux-x64.tar.gz", "if-no-files-found" => "error" },
      },
      {
        "name" => "Keep the agent disk for the other targets' builds", "uses" => "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "with" => { "name" => "desktop-agent-disk", "path" => "out/desktop/agent.img", "if-no-files-found" => "error" },
      },
    ], job.fetch("steps")
    # The tarball's hash and its size are the job's own outputs, which no other job of the run can
    # set: an artifact is the run's, and any of its jobs may put another file under the tarball's
    # name. The signing opens no tarball, and has these two words for what it signs.
    assert_equal({ "sha256" => "${{ steps.tarball.outputs.sha256 }}", "size" => "${{ steps.tarball.outputs.size }}" }, job.fetch("outputs"))
  end

  def test_desktop_describe_reads_the_built_tarball_in_a_job_that_holds_no_secret_and_says_one_word_of_it
    job = @workflow.fetch("jobs").fetch("desktop-describe")
    steps = job.fetch("steps")

    # All that reads the build's tarball, on a runner of its own: a step of the job that signs
    # could write into that job's checkout, or leave something running beside its key. The job
    # is these three steps, whole, each action by its commit (v4.4.0, v4.3.0).
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "name" => "Take the tarball", "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093", "with" => { "name" => "desktop-tarball", "path" => "out/desktop" } },
      {
        "name" => "Write its manifest, and say the app's state schema to the publish job",
        "id" => "manifest",
        # The hash the build's job gave for its tarball, through the step's environment, never
        # pasted into its script, where what the build says would be run.
        "env" => { "DESKTOP_TARBALL_SHA256" => "${{ needs.desktop-build.outputs.sha256 }}" },
        "run" => "desktop/release/publish.sh describe \"${GITHUB_REF_NAME#v}\" out/desktop\necho \"schema=$(jq -r .stateSchema out/desktop/manifest.json)\" >>\"$GITHUB_OUTPUT\"\n",
      },
    ], steps
    # One word is handed on, as the job's own output, which no other job can set: the state schema.
    # Nothing of this job's is an artifact, which is the run's.
    assert_equal({ "schema" => "${{ steps.manifest.outputs.schema }}" }, job.fetch("outputs"))
    # No secret by any name, no Environment, which would hand it every secret of that one, and no
    # env of the job's own; a runner of its own; and the build before it, whose hash it checks.
    assert_equal %w[needs outputs permissions runs-on steps timeout-minutes], job.keys.sort
    assert_equal ["desktop-build"], Array(job.fetch("needs"))
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
    refute_match(/secrets/i, job.to_s)
    refute_match(/DESKTOP_RELEASE_KEY/i, job.to_s)
    refute_match(/desktop-release/i, job.to_s)
  end

  def test_desktop_publish_signs_and_sends_the_built_tarball_one_release_at_a_time_in_its_environment
    job = @workflow.fetch("jobs").fetch("desktop-publish")
    steps = job.fetch("steps")
    runs = steps.map { |step| step["run"].to_s }

    # Both: the build, for its two words of the tarball, and the job that read it, for its one.
    assert_equal %w[desktop-build desktop-describe], Array(job.fetch("needs"))
    assert_equal "desktop-release", job.fetch("environment")
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal({ "group" => "desktop-release", "cancel-in-progress" => false }, job.fetch("concurrency"))
    taken = steps.each_index.select { |index| steps[index]["uses"].to_s.start_with?("actions/download-artifact@") }
    # The tarball alone is taken, to be sent: the manifest is written here, and no other job's file is read for it.
    assert_equal [{ "name" => "desktop-tarball", "path" => "out/desktop" }], taken.map { |index| steps[index].fetch("with") }
    sign = runs.index { |run| run.include?('desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop') }
    send = runs.index { |run| run.include?('desktop/release/publish.sh send "${GITHUB_REF_NAME#v}" out/desktop') }
    refute_nil sign
    refute_nil send
    # The tarball is here before its manifest is signed, and that is signed before both are sent.
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
        # With the key, three words that are each a job's own output: the build's for its tarball,
        # its hash and its size, and the describe job's for the app's state schema. The signing
        # opens no tarball, and reads no artifact.
        assert_equal({
          "DESKTOP_RELEASE_KEY" => "${{ secrets.DESKTOP_RELEASE_KEY }}",
          "DESKTOP_TARBALL_SHA256" => "${{ needs.desktop-build.outputs.sha256 }}",
          "DESKTOP_TARBALL_SIZE" => "${{ needs.desktop-build.outputs.size }}",
          "DESKTOP_STATE_SCHEMA" => "${{ needs.desktop-describe.outputs.schema }}",
        }, step.fetch("env"))
      elsif run.include?("publish.sh send")
        assert_equal r2, step.fetch("env")
      else
        refute step.to_s.include?("secrets."), "#{step["name"] || step["uses"]} reads a secret"
      end
    end
    # Windows's publish holds it as Linux's does (test_windows_s_publish_signs_and_sends_the_signed_msi_…), and no other job.
    jobs.except("desktop-publish", "desktop-win-publish").each { |name, job| refute job.to_s.include?("DESKTOP_RELEASE_KEY"), "#{name} reads the release key" }
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

    %w[desktop-build desktop-describe desktop-win-build desktop-win-sign desktop-win-msi desktop-win-sign-msi desktop-win-describe desktop-publish desktop-win-publish].each do |name|
      assert jobs.fetch(name).key?("timeout-minutes"), "#{name} is not bounded"
    end
  end

  def test_the_cloud_release_does_not_wait_for_the_desktop
    release_needs = Array(@workflow.fetch("jobs").fetch("release").fetch("needs", []))

    refute_includes release_needs, "desktop-build"
    refute_includes release_needs, "desktop-describe"
    refute_includes release_needs, "desktop-publish"
  end

  def test_desktop_publish_is_the_tag_s_checkout_the_tarball_s_download_and_publish_sh_and_nothing_else
    job = @workflow.fetch("jobs").fetch("desktop-publish")
    steps = job.fetch("steps")

    # A step need not name npm to run it: package.sh does, and so may an action, a container's
    # image, or a runner that kept what an earlier job's npm left on it. The job that holds the
    # keys is these four steps, each with these keys alone, on a runner of its own.
    # Its two actions are named by their commits (v4.4.0 and v4.3.0): a tag can be moved to other
    # code, and the job that holds the release key would run it.
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" },
      { "run" => 'desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop' },
      { "run" => 'desktop/release/publish.sh send "${GITHUB_REF_NAME#v}" out/desktop' },
    ], steps.map { |step| step.slice("uses", "run") }
    # The checkout names no ref, repository or path: publish.sh and install.sh are the tag's.
    assert_equal [%w[uses], %w[name uses with], %w[env name run], %w[env name run]], steps.map { |step| step.keys.sort }
    assert_equal %w[concurrency environment needs permissions runs-on steps timeout-minutes], job.keys.sort
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
  end

  # The Windows release's jobs, by name.
  WINDOWS_BUILDS = %w[desktop-win-build desktop-win-msi].freeze
  WINDOWS_SIGNINGS = %w[desktop-win-sign desktop-win-sign-msi].freeze
  # The run's one word for how it signs the Windows app, and the signer it names, from the job that decides them once.
  MODE = "needs.desktop-signing-mode.outputs.signing"
  SIGNER = "${{ needs.desktop-signing-mode.outputs.signer }}"
  ROOT = "${{ #{MODE} == 'account' && 'account' || steps.trial.outputs.root }}"

  def test_the_signing_is_decided_once_for_the_run_by_one_job_and_every_windows_job_and_rollout_takes_it_from_there
    jobs = @workflow.fetch("jobs")
    mode = jobs.fetch("desktop-signing-mode")
    # One step that reads the two variables and says three words: it holds nothing, needs nothing and reads nothing.
    assert_equal %w[outputs permissions runs-on steps timeout-minutes], mode.keys.sort
    assert_equal({}, mode.fetch("permissions"))
    assert_equal({ "signing" => "${{ steps.mode.outputs.signing }}", "account" => "${{ steps.mode.outputs.account }}", "signer" => "${{ steps.mode.outputs.signer }}" }, mode.fetch("outputs"))
    assert_equal 1, mode.fetch("steps").length
    step = mode.fetch("steps").first
    assert_equal({ "ACCOUNT" => "${{ vars.ARTIFACT_SIGNING_ACCOUNT }}", "SUBJECT" => "${{ vars.ARTIFACT_SIGNING_SUBJECT }}" }, step.fetch("env"))
    assert_includes step.fetch("run"), "printf 'signing=account\\naccount=%s\\nsigner=%s\\n' \"$ACCOUNT\" \"$SUBJECT\""
    assert_includes step.fetch("run"), "printf 'signing=trial\\naccount=\\nsigner=CN=Surogate trial signer\\n'"
    # No other job reads either variable: each that signs, makes the MSI, publishes Windows or rolls out needs the
    # deciding job and reads its words, so a job re-run later does as the run began.
    readers = jobs.select { |_, job| job.to_s.match?(/vars\.ARTIFACT_SIGNING_(ACCOUNT|SUBJECT)\b/) }.keys
    assert_equal ["desktop-signing-mode"], readers
    %w[desktop-win-sign desktop-win-msi desktop-win-sign-msi desktop-win-publish rollout rollout-with-windows].each do |name|
      assert_includes Array(jobs.fetch(name).fetch("needs")), "desktop-signing-mode", name
    end
    (jobs.keys - ["desktop-signing-mode"]).each { |name| refute_match(/ARTIFACT_SIGNING_ACCOUNT|ARTIFACT_SIGNING_SUBJECT/, jobs.fetch(name).to_s, name) }
    # The publish of Windows and the rollout that waits for it, by that word alone.
    assert_equal "#{MODE} == 'account'", jobs.fetch("desktop-win-publish").fetch("if")
  end

  def test_the_windows_app_is_built_and_its_msi_made_on_windows_by_jobs_that_hold_nothing_to_sign_with
    jobs = @workflow.fetch("jobs")
    # npm's and Cargo's install scripts, and WiX, run in these: no secret, no Environment, no token to sign with, and a
    # token that reads the repository alone, kept nowhere their steps can read it.
    WINDOWS_BUILDS.each do |name|
      job = jobs.fetch(name)
      assert_equal "windows-2025", job.fetch("runs-on"), name
      assert_equal({ "contents" => "read" }, job.fetch("permissions"), name)
      refute job.key?("environment"), name
      refute_match(/secrets|id-token|vars\./, job.to_s, name)
      refute_match(/ARTIFACT_SIGNING/, job.to_s, name)
      checkouts = job.fetch("steps").select { |step| step["uses"].to_s.start_with?("actions/checkout@") }
      assert_equal [{ "persist-credentials" => false }], checkouts.map { |step| step.fetch("with") }, name
      assert_equal "git config --global core.autocrlf false", job.fetch("steps").first.fetch("run"), name
    end
    assert_equal ["desktop-build"], Array(jobs.fetch("desktop-win-build").fetch("needs"))
    assert_equal %w[desktop-win-sign desktop-signing-mode], Array(jobs.fetch("desktop-win-msi").fetch("needs"))
    runs = jobs.fetch("desktop-win-build").fetch("steps").map { |step| step["run"].to_s.strip }
    # The layout of the tag's app, its agent disk the one desktop-build made, and its VM manifest the release's image's.
    assert_equal [
      "npm ci", "npm run electron:install", "npm run build:win32",
      'scripts/package-win.sh "${GITHUB_REF_NAME#v}" ../out/vm/manifest.json ../out/agent/agent.img ../out/win',
    ], runs.select { |run| run.start_with?("npm", "scripts/") }
    taken = jobs.fetch("desktop-win-build").fetch("steps").select { |step| step["uses"].to_s.start_with?("actions/download-artifact@") }
    assert_equal [{ "name" => "desktop-vm-manifest", "path" => "out/vm" }, { "name" => "desktop-agent-disk", "path" => "out/agent" }], taken.map { |step| step.fetch("with") }
    # The MSI of the signed layout, by WiX as msi-win.ps1 pins it, once each program is found the run's signer's alone.
    msi_steps = jobs.fetch("desktop-win-msi").fetch("steps")
    signed_by = msi_steps.index { |step| step["run"].to_s.strip == "desktop/scripts/sign-win.ps1 signed-by out/win $env:SIGNER" }
    refute_nil signed_by
    assert_equal({ "SIGNER" => SIGNER }, msi_steps[signed_by].fetch("env"))
    assert_operator signed_by, :<, msi_steps.index { |step| step["run"].to_s.include?("msi-win.ps1") }
    made = msi_steps.map { |step| step["run"].to_s }.join
    assert_includes made, 'desktop/scripts/msi-win.ps1 $version out/win/Surogate out/win/msi "out/msi/surogate-desktop-$version-win32-x64.msi"'
  end

  def test_the_windows_signing_signs_with_artifact_signing_through_oidc_once_the_repository_names_its_account_and_runs_no_build_tool
    jobs = @workflow.fetch("jobs")
    WINDOWS_SIGNINGS.each do |name|
      job = jobs.fetch(name)
      steps = job.fetch("steps")
      assert_equal "windows-2025", job.fetch("runs-on"), name
      assert_equal "desktop-windows-signing", job.fetch("environment"), name
      # OpenID Connect's token to sign with, and the repository's to read the tag: no stored secret at all.
      assert_equal({ "contents" => "read", "id-token" => "write" }, job.fetch("permissions"), name)
      refute_match(/secrets/i, job.to_s, name)
      # No step installs or runs a build tool, whose packages' scripts would run beside the token.
      steps.each do |step|
        refute_match(/\b(npm|npx|node|cargo|dotnet|wix|msi-win)\b/i, step["run"].to_s, "#{name}: #{step["name"]}")
        refute_match(/setup-node|setup-dotnet|rust/, step["uses"].to_s, "#{name}: #{step["name"]}")
      end
      # Artifact Signing's own action, after Azure's sign-in by the federated credential, only once the account is named.
      azure = steps.select { |step| step["uses"].to_s.start_with?("azure/") }
      assert_equal %w[azure/login azure/artifact-signing-action], azure.map { |step| step["uses"].split("@").first }, name
      azure.each { |step| assert_equal "#{MODE} == 'account'", step.fetch("if"), "#{name}: #{step["name"]}" }
      assert_equal({ "client-id" => "${{ vars.AZURE_CLIENT_ID }}", "tenant-id" => "${{ vars.AZURE_TENANT_ID }}", "allow-no-subscriptions" => true }, azure.first.fetch("with"), name)
      signing = azure.last.fetch("with")
      assert_equal({
        "endpoint" => "${{ vars.ARTIFACT_SIGNING_ENDPOINT }}", "signing-account-name" => "${{ needs.desktop-signing-mode.outputs.account }}",
        "certificate-profile-name" => "${{ vars.ARTIFACT_SIGNING_CERTIFICATE_PROFILE }}", "file-digest" => "SHA256",
        "timestamp-rfc3161" => "http://timestamp.acs.microsoft.com", "timestamp-digest" => "SHA256", "cache-dependencies" => false,
      }, signing.except("files-catalog", *signing.keys.grep(/^exclude-/)), name)
      # Signed in by Azure's command line alone, as Azure's sign-in left it.
      assert_equal ["exclude-azure-cli-credential"], signing.select { |key, value| key.start_with?("exclude-") && value == false }.keys, name
      # Else this run's own certificate, its root trusted for the job alone; every signature checked either way, by
      # Windows's own trust, a trial's against its run's root alone.
      trial = steps.find { |step| step["id"] == "trial" }
      assert_equal "#{MODE} == 'trial'", trial.fetch("if"), name
      assert_equal({ "SIGNER" => SIGNER }, trial.fetch("env"), name)
      assert_match(%r{\$root = desktop/scripts/sign-win\.ps1 trial out/\w+\.txt \$env:SIGNER}, trial.fetch("run"), name)
      # Checked by the run's signer and the root it was signed under: the account's public root, or the trial's.
      verify = steps.find { |step| step["run"].to_s.include?("sign-win.ps1 verify") }
      refute verify.key?("if"), name
      assert_equal({ "SIGNER" => SIGNER, "ROOT" => ROOT }, verify.fetch("env"), name)
      assert_match(/sign-win\.ps1 verify out\/\w+ \$env:SIGNER \$env:ROOT/, verify.fetch("run"), name)
      untrust = steps.find { |step| step["run"].to_s.include?("sign-win.ps1 untrust") }
      assert_equal "always() && steps.trial.outputs.root != ''", untrust.fetch("if"), name
      [azure.last, trial].each { |step| assert_operator steps.index(step), :<, steps.index(verify), name }
      catalog = steps.find { |step| step["run"].to_s.include?("sign-win.ps1 list") }.fetch("run")
      assert_includes signing.fetch("files-catalog"), catalog.split.last.tr("/", "\\"), name
    end
    assert_equal %w[desktop-win-build desktop-signing-mode], Array(jobs.fetch("desktop-win-sign").fetch("needs"))
    assert_equal %w[desktop-win-msi desktop-signing-mode], Array(jobs.fetch("desktop-win-sign-msi").fetch("needs"))
    # The MSI's signing checks what the MSI holds, after the MSI's own signature and before its words are said: the
    # signed layout, which it takes, byte for byte, every program the run's signer's.
    steps = jobs.fetch("desktop-win-sign-msi").fetch("steps")
    contents = steps.index { |step| step["run"].to_s.include?("sign-win.ps1 contents") }
    refute_nil contents
    assert_equal({ "SIGNER" => SIGNER, "ROOT" => ROOT }, steps[contents].fetch("env"))
    assert_equal %(desktop/scripts/sign-win.ps1 contents "out/msi/surogate-desktop-$($env:GITHUB_REF_NAME -replace '^v', '')-win32-x64.msi" out/win/Surogate $env:SIGNER $env:ROOT), steps[contents].fetch("run").strip
    refute steps[contents].key?("if")
    assert_operator steps.index { |step| step["run"].to_s.include?("sign-win.ps1 verify") }, :<, contents
    assert_operator contents, :<, steps.index { |step| step["id"] == "msi" }
    taken = steps.select { |step| step["uses"].to_s.start_with?("actions/download-artifact@") }.map { |step| step.fetch("with") }
    assert_equal [{ "name" => "desktop-win-msi-unsigned", "path" => "out/msi" }, { "name" => "desktop-win-signed", "path" => "out/win" }], taken
    # Every variable the release reads, by its name: none is a secret, and none is given a value here.
    assert_equal %w[ARTIFACT_SIGNING_ACCOUNT ARTIFACT_SIGNING_CERTIFICATE_PROFILE ARTIFACT_SIGNING_ENDPOINT ARTIFACT_SIGNING_SUBJECT AZURE_CLIENT_ID AZURE_TENANT_ID],
                 File.read(".github/workflows/release.yml").gsub(/^\s*#.*$/, "").scan(/\bvars\.([A-Z_]+)/).flatten.uniq.sort
  end

  def test_no_msi_a_trial_signed_is_kept_or_published_and_windows_is_published_only_once_its_account_is_named
    jobs = @workflow.fetch("jobs")
    # The signed MSI is handed on only by the step that runs where Artifact Signing signed it.
    kept = jobs.fetch("desktop-win-sign-msi").fetch("steps").select { |step| step["uses"].to_s.start_with?("actions/upload-artifact@") }
    assert_equal [["#{MODE} == 'account'", "desktop-win-msi"]], kept.map { |step| [step["if"], step.fetch("with").fetch("name")] }
    # Its hash and size are the signing job's own words, of the MSI it checked.
    assert_equal({ "sha256" => "${{ steps.msi.outputs.sha256 }}", "size" => "${{ steps.msi.outputs.size }}" }, jobs.fetch("desktop-win-sign-msi").fetch("outputs"))
    steps = jobs.fetch("desktop-win-sign-msi").fetch("steps")
    said = steps.index { |step| step["id"] == "msi" }
    assert_operator steps.index { |step| step["run"].to_s.include?("sign-win.ps1 verify") }, :<, said
    # And it is taken by Windows's publish alone, which runs only once the account is named.
    takers = jobs.select { |_, job| job.fetch("steps", []).any? { |step| step["uses"].to_s.start_with?("actions/download-artifact@") && step.dig("with", "name") == "desktop-win-msi" } }.keys
    assert_equal ["desktop-win-publish"], takers
    assert_equal "#{MODE} == 'account'", jobs.fetch("desktop-win-publish").fetch("if")
  end

  def test_windows_s_publish_signs_and_sends_the_signed_msi_by_its_signing_job_s_words_in_the_release_s_environment
    jobs = @workflow.fetch("jobs")
    job = jobs.fetch("desktop-win-publish")
    steps = job.fetch("steps")
    r2 = {
      "S3_ENDPOINT" => "${{ secrets.R2_ENDPOINT }}",
      "S3_BUCKET" => "${{ secrets.R2_BUCKET }}",
      "AWS_ACCESS_KEY_ID" => "${{ secrets.R2_ACCESS_KEY_ID }}",
      "AWS_SECRET_ACCESS_KEY" => "${{ secrets.R2_SECRET_ACCESS_KEY }}",
    }
    assert_equal %w[desktop-win-sign-msi desktop-win-describe desktop-signing-mode], Array(job.fetch("needs"))
    assert_equal "desktop-release", job.fetch("environment")
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal({ "group" => "desktop-release-win32-x64", "cancel-in-progress" => false }, job.fetch("concurrency"))
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
    assert_equal %w[concurrency environment if needs permissions runs-on steps timeout-minutes], job.keys.sort
    # The tag's checkout, the MSI's download, and publish.sh's sign and send for Windows's target: nothing else, no npm,
    # and no archive opened.
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" },
      { "run" => 'desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop' },
      { "run" => 'desktop/release/publish.sh send "${GITHUB_REF_NAME#v}" out/desktop' },
    ], steps.map { |step| step.slice("uses", "run") }
    assert_equal [%w[uses], %w[name uses with], %w[env name run], %w[env name run]], steps.map { |step| step.keys.sort }
    assert_equal({ "name" => "desktop-win-msi", "path" => "out/desktop" }, steps[1].fetch("with"))
    assert_equal({
      "DESKTOP_TARGET" => "win32-x64",
      "DESKTOP_RELEASE_KEY" => "${{ secrets.DESKTOP_RELEASE_KEY }}",
      "DESKTOP_TARBALL_SHA256" => "${{ needs.desktop-win-sign-msi.outputs.sha256 }}",
      "DESKTOP_TARBALL_SIZE" => "${{ needs.desktop-win-sign-msi.outputs.size }}",
      "DESKTOP_STATE_SCHEMA" => "${{ needs.desktop-win-describe.outputs.schema }}",
    }, steps[2].fetch("env"))
    assert_equal({ "DESKTOP_TARGET" => "win32-x64", **r2 }, steps[3].fetch("env"))
    refute job.key?("env")
    # The release key reaches the two publish jobs' signing steps alone.
    keyed = jobs.flat_map { |name, each| each.fetch("steps", []).select { |step| step.to_s.include?("DESKTOP_RELEASE_KEY") }.map { |step| [name, step["name"]] } }
    assert_equal [["desktop-publish", "Sign its manifest"], ["desktop-win-publish", "Sign its manifest"]], keyed
  end

  def test_the_windows_layout_is_described_with_no_secret_and_its_schema_must_be_the_linux_app_s
    job = @workflow.fetch("jobs").fetch("desktop-win-describe")
    assert_equal %w[desktop-describe desktop-win-sign-msi], Array(job.fetch("needs"))
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal %w[needs outputs permissions runs-on steps timeout-minutes], job.keys.sort
    refute_match(/secrets|DESKTOP_RELEASE_KEY|desktop-release|vars\./i, job.to_s)
    taken = job.fetch("steps").select { |step| step["uses"].to_s.start_with?("actions/download-artifact@") }
    assert_equal [{ "name" => "desktop-win-signed", "path" => "out/win" }], taken.map { |step| step.fetch("with") }
    step = job.fetch("steps").find { |each| each["id"] == "manifest" }
    assert_equal({
      "DESKTOP_TARGET" => "win32-x64",
      "DESKTOP_TARBALL_SHA256" => "${{ needs.desktop-win-sign-msi.outputs.sha256 }}",
      "DESKTOP_TARBALL_SIZE" => "${{ needs.desktop-win-sign-msi.outputs.size }}",
      "LINUX_STATE_SCHEMA" => "${{ needs.desktop-describe.outputs.schema }}",
    }, step.fetch("env"))
    run = step.fetch("run")
    assert_includes run, 'desktop/release/publish.sh describe "${GITHUB_REF_NAME#v}" out/win'
    assert_includes run, '[ "$schema" = "$LINUX_STATE_SCHEMA" ] || {'
    assert_equal({ "schema" => "${{ steps.manifest.outputs.schema }}" }, job.fetch("outputs"))
  end

  def test_the_desktop_s_secrets_reach_no_other_job_however_they_are_named
    jobs = @workflow.fetch("jobs")

    # An Environment's secrets reach every job that names it, all of them at once through
    # toJSON(secrets), which names none.
    jobs.except("desktop-publish", "desktop-win-publish").each do |name, job|
      refute_includes job["environment"].to_s, "desktop-release", "#{name} runs in the desktop's Environment"
    end
    # secrets.X, secrets['X'] or all of them: the build names no secret in any form.
    refute_match(/\bsecrets\b/, jobs.fetch("desktop-build").to_s)
    # R2's keys can rewrite desktop/install.sh, which runs as root on each new install: the guest
    # image's job and the desktop's two publish jobs read them, and no other.
    r2 = /\bR2_(ENDPOINT|BUCKET|ACCESS_KEY_ID|SECRET_ACCESS_KEY)\b/
    assert_equal %w[desktop-vm-image desktop-publish desktop-win-publish], jobs.select { |_, job| job.to_s.match?(r2) }.keys
  end

  def test_the_macos_suites_run_by_hand_on_a_mac_on_the_ref_they_are_given_and_hold_nothing_to_leak
    macos = YAML.load_file(MACOS)
    on = triggers(macos)
    # GitHub bills a macOS runner's minutes at a multiple of Linux's, and each suite runs once a
    # branch, on its final head: started by hand, never by a push or a pull request.
    assert_equal %w[workflow_dispatch], on.keys
    inputs = on.fetch("workflow_dispatch").fetch("inputs")
    assert_equal %w[ref], inputs.keys
    assert_equal [true, "string"], inputs.fetch("ref").values_at("required", "type")
    # Its token reads the repository and nothing else, and nothing of the workflow's own is every job's.
    assert_equal ["jobs", "name", "permissions", "true"], macos.keys.map(&:to_s).sort
    assert_equal({ "contents" => "read" }, macos.fetch("permissions"))
    jobs = macos.fetch("jobs")
    assert_equal %w[unit file-host vm-native shell app], jobs.keys
    jobs.each do |name, job|
      assert_equal "macos-15", job.fetch("runs-on"), name
      assert job.key?("timeout-minutes"), "#{name} is not bounded"
      # A run on a branch's head runs that branch's code: no job is given a secret, in any form,
      # an Environment's or a permission of its own.
      refute_match(/secrets/i, job.to_s, "#{name} names a secret")
      refute job.key?("environment"), "#{name} runs in an Environment"
      refute job.key?("permissions"), "#{name} asks for permissions of its own"
    end
    # The ref it is given, checked out with no token kept for that ref's code to use, by every job.
    jobs.each { |name, job| assert_equal({ "ref" => "${{ inputs.ref }}", "persist-credentials" => false }, job.fetch("steps").first.fetch("with"), name) }
    steps = jobs.fetch("unit").fetch("steps")
    # The files of the desktop's vitest project for macOS, which each part built for macOS joins.
    assert_equal "npx vitest run --project macos", steps.last.fetch("run").strip
  end

  def test_the_macos_app_is_laid_out_checked_and_signed_by_the_run_s_own_identity_and_nothing_but_the_release_notarizes_or_assesses_it
    jobs = YAML.load_file(MACOS).fetch("jobs")
    app = jobs.fetch("app")
    steps = app.fetch("steps")
    runs = steps.map { |step| step["run"].to_s }
    # The bundle as the release makes it, with a disk of zeros and an arm64 manifest's form in the guest's place, then
    # its layout's test, the signing script's own tests, and the app signed with the run's identity and checked as the
    # release checks its own, packed, and put in a disk image that is checked: in that order, each step gating.
    order = [
      "scripts/mac-build.sh", "scripts/package-mac.sh 0.1.0 ../out/stand-in/manifest.json ../out/stand-in/agent.img ../out/mac",
      "test/package-mac.test.ts", "test/sign-mac.test.sh",
      %(identity="$(scripts/sign-mac.sh trial "$keychain" 'Surogate trial signer')"),
      'scripts/sign-mac.sh sign ../out/mac/Surogate.app "$identity" "$keychain"',
      "scripts/sign-mac.sh verify ../out/mac/Surogate.app trial 'Surogate trial signer'",
      "scripts/sign-mac.sh pack ../out/mac/Surogate.app ../out/mac/surogate-desktop-0.1.0-darwin-arm64.tar.gz",
      'scripts/sign-mac.sh dmg ../out/mac/Surogate.app ../out/mac/surogate-desktop-0.1.0-darwin-arm64.dmg "$identity" "$keychain"',
      "scripts/sign-mac.sh dmg-check ../out/mac/surogate-desktop-0.1.0-darwin-arm64.dmg ../out/mac/Surogate.app trial 'Surogate trial signer'",
      "WebAssembly.Module",
    ].map { |part| runs.index { |run| run.include?(part) } }
    refute_includes order, nil
    assert_equal order.sort, order
    assert_includes runs[order.first], "scripts/mac-build.sh"
    refute app.key?("continue-on-error")
    # The layout's test is told the bundle and what it was made of.
    check = steps.find { |step| step["run"].to_s.include?("test/package-mac.test.ts") }
    assert_equal %w[SUROGATE_MAC_AGENT_DISK SUROGATE_MAC_LAYOUT SUROGATE_MAC_VERSION SUROGATE_MAC_VM_MANIFEST], check.fetch("env").keys.sort
    # Whether the signed node loads fsat is recorded, never gating, and is the one step that does not gate.
    assert_equal ["Record whether the signed node loads fsat"], steps.select { |step| step["continue-on-error"] }.map { |step| step["name"] }
    # The run's keychain is gone at the job's end, however it ends.
    assert_equal "always()", steps.last.fetch("if")
    assert_includes steps.last.fetch("run"), 'scripts/sign-mac.sh forget "$KEYCHAIN"'
    assert_equal({ "KEYCHAIN" => "${{ steps.trial.outputs.keychain }}" }, steps.last.fetch("env"))
    # Notarization, Gatekeeper's assessment and the Developer ID's import are the release's signing job's alone, in
    # every workflow file: a run of a branch's code is never handed Apple's keys.
    workflows.each do |file, workflow|
      workflow.fetch("jobs").each do |name, job|
        used = job.fetch("steps", []).flat_map { |step| step["run"].to_s.scan(/sign-mac\.sh (notarize|assess|import)\b/).flatten }.uniq.sort
        expected = file == "release.yml" && name == "desktop-mac-sign" ? %w[assess import notarize] : []
        assert_equal expected, used, "#{file}: #{name}"
      end
    end
  end

  def test_the_windows_suites_run_by_hand_or_for_the_release_on_windows_on_the_ref_they_are_given_and_hold_nothing_to_leak
    windows = YAML.load_file(WINDOWS)
    on = triggers(windows)
    # Each suite runs once a branch, on its final head, started by hand; or for the release, which calls it on its
    # tag. Never by a push or a pull request, whose code it would run.
    assert_equal %w[workflow_dispatch workflow_call], on.keys
    on.each do |trigger, given|
      # The ref it is given, and nothing else: no secret is ever handed to it, and it hands nothing back.
      assert_equal %w[inputs], given.keys, trigger
      inputs = given.fetch("inputs")
      assert_equal %w[ref], inputs.keys, trigger
      assert_equal [true, "string"], inputs.fetch("ref").values_at("required", "type"), trigger
    end
    # Its token reads the repository and nothing else, and nothing of the workflow's own is every job's.
    assert_equal ["jobs", "name", "permissions", "true"], windows.keys.map(&:to_s).sort
    assert_equal({ "contents" => "read" }, windows.fetch("permissions"))
    jobs = windows.fetch("jobs")
    assert_equal %w[unit file-host vm-probe vm-native vm-backend vm-guest vm-boot msi shell], jobs.keys
    jobs.each do |name, job|
      # On Windows, x64; but for the guest that vm-boot boots, which Docker builds on Linux, as the release builds it.
      assert_equal name == "vm-guest" ? "blacksmith-4vcpu-ubuntu-2404" : "windows-2025", job.fetch("runs-on"), name
      assert job.key?("timeout-minutes"), "#{name} is not bounded"
      # A run on a branch's head runs that branch's code: no job is given a secret, in any form,
      # an Environment's or a permission of its own, a signing token's among them.
      refute_match(/secrets/i, job.to_s, "#{name} names a secret")
      refute_match(/id-token/, job.to_s, "#{name} asks for a token to sign with")
      refute job.key?("environment"), "#{name} runs in an Environment"
      refute job.key?("permissions"), "#{name} asks for permissions of its own"
      steps = job.fetch("steps")
      # The ref it is given, checked out with no token kept for that ref's code to use, as its files are written.
      checkouts = steps.select { |step| step["uses"].to_s.start_with?("actions/checkout@") }
      assert_equal [{ "ref" => "${{ inputs.ref }}", "persist-credentials" => false }], checkouts.map { |step| step.fetch("with") }, name
      # Git for Windows' line ends, which Linux's git leaves as they are written.
      next if name == "vm-guest"

      assert_operator steps.index { |step| step["run"].to_s.include?("git config --global core.autocrlf false") }, :<, steps.index(checkouts.first), name
    end
    refute_match(/id-token/, windows.to_s)
    # The files of the desktop's vitest project for Windows, which each part built for Windows joins; they gate.
    # npm run build is the Linux app's build, which refuses on Windows: no job on a Windows runner runs it.
    jobs.each do |name, job|
      next if name == "vm-guest"

      job.fetch("steps").each { |step| refute_match(/npm run build(?!:)/, step["run"].to_s, "#{name} runs the Linux app's build") }
    end
    unit = jobs.fetch("unit")
    refute unit.key?("continue-on-error")
    assert_equal "npx vitest run --project windows", unit.fetch("steps").last.fetch("run").strip
    # The file host's suites on the runner's own NTFS gate, each step bounded, after ntat and rg are built.
    host = jobs.fetch("file-host")
    refute host.key?("continue-on-error")
    host_runs = host.fetch("steps").map { |step| step["run"].to_s }
    assert_includes host_runs, "npm run build:win32"
    suites = host.fetch("steps").select { |step| step["run"].to_s.include?("npx vitest run") }
    assert_equal 4, suites.size
    suites.each { |step| assert step.key?("timeout-minutes"), step["name"] }
    # The shell's and the browser's suites, which gate: the helper's lints and its tests, those that fill the
    # runner's stores among them, built as its lock file has it; the app end to end; the browser host on Chrome and Edge.
    shell = jobs.fetch("shell")
    refute shell.key?("continue-on-error")
    shell_runs = shell.fetch("steps").map { |step| step["run"].to_s.strip }
    assert_includes shell_runs, "cargo clippy --locked --all-targets -- -D warnings"
    assert_includes shell_runs, "cargo test --locked -- --include-ignored"
    assert_includes shell_runs, "npx vitest run -c vitest.e2e.config.ts test/e2e/windows.e2e.ts"
    browsers = shell.fetch("steps").select { |step| step["run"].to_s.include?("browser-host-win32.test.ts") }.map { |step| step.fetch("env").fetch("SUROGATE_TEST_BROWSER") }
    assert_equal ['C:\Program Files\Google\Chrome\Application\chrome.exe', 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'], browsers
    # What the runner offers the VM, recorded and never gating: its Rust probe built as its lock file has it.
    probe = jobs.fetch("vm-probe")
    assert_equal true, probe.fetch("continue-on-error")
    runs = probe.fetch("steps").map { |step| step["run"].to_s }
    assert(runs.any? { |run| run.include?("Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform") })
    assert(runs.any? { |run| run.include?("cargo build --release --locked") })
    # The native programs gate; the real boot never does.
    native = jobs.fetch("vm-native")
    refute native.key?("continue-on-error")
    assert(native.fetch("steps").map { |step| step["run"].to_s }.any? { |run| run.include?("cargo clippy --locked --workspace --all-targets -- -D warnings") })
    assert(native.fetch("steps").map { |step| step["run"].to_s }.any? { |run| run.include?("cargo test --locked --workspace") })
    # The TypeScript backend gates too; it builds the desktop the stand-in imports.
    backend = jobs.fetch("vm-backend")
    refute backend.key?("continue-on-error")
    assert_equal "npx vitest run test/vm-win32.test.ts", backend.fetch("steps").last.fetch("run").strip
    # The Windows app as the release makes it gates: laid out and checked, every program signed with the run's own
    # certificate and checked as the release checks its own, the MSI and a later one made, signed and checked, and
    # installed, checked and removed, in that order; and the run's roots trusted no more at the job's end, however it ends.
    msi = jobs.fetch("msi")
    refute msi.key?("continue-on-error")
    msi_runs = msi.fetch("steps").map { |step| step["run"].to_s }
    order = [
      "npm run build:win32", "scripts/package-win.sh 0.1.0", "test/package-win.test.ts", "./scripts/sign-win.ps1 trial ..\\out\\programs.txt 'CN=Surogate trial signer'",
      "./scripts/sign-win.ps1 verify ..\\out\\win 'CN=Surogate trial signer' $root", "./scripts/sign-win.ps1 signed-by ..\\out\\win 'CN=Surogate trial signer'",
      "./scripts/msi-win.ps1 0.1.1", "./scripts/sign-win.ps1 verify ..\\out\\msi 'CN=Surogate trial signer' $root",
      "./scripts/sign-win.ps1 contents ..\\out\\msi\\surogate-desktop-0.1.0-win32-x64.msi ..\\out\\win\\Surogate 'CN=Surogate trial signer' $root",
      "$root = ./scripts/sign-win.ps1 trial ..\\out\\mixed.txt 'CN=Surogate account stand-in'", "./test/msi.test.ps1",
    ].map { |part| msi_runs.index { |run| run.include?(part) } }
    refute_includes order, nil
    assert_equal order.sort, order
    # An MSI signed around programs another signed is refused by the release's own check of what an MSI holds.
    mixed = msi.fetch("steps").find { |step| step["id"] == "mixed" }.fetch("run")
    assert_includes mixed, "./scripts/sign-win.ps1 contents ..\\out\\mixed\\surogate-desktop-0.1.0-win32-x64.msi ..\\out\\win\\Surogate 'CN=Surogate account stand-in' $root"
    assert_includes mixed, "if (-not $refused) { throw 'The check of what an MSI holds took an MSI of programs another signed' }"
    assert_equal "always()", msi.fetch("steps").last.fetch("if")
    assert_includes msi.fetch("steps").last.fetch("run"), "./scripts/sign-win.ps1 untrust $root"
    # The real boot never gates, nor does the build of what it boots: the x64 guest's image, unpacked as the
    # measurements boot it, and the agent disk, each built on Linux and kept a day, for this run's boot alone.
    guest = jobs.fetch("vm-guest")
    assert_equal true, guest.fetch("continue-on-error")
    assert_operator guest.fetch("timeout-minutes"), :<=, 90
    guest_runs = guest.fetch("steps").map { |step| step["run"].to_s.strip }
    assert_includes guest_runs, "images/guest/build.sh --arch amd64 images/guest/out"
    assert_includes guest_runs, "npm run agent-disk"
    kept = jobs.to_h { |name, job| [name, job.fetch("steps").select { |step| step["uses"].to_s.start_with?("actions/upload-artifact@") }.map { |step| step.fetch("with") }] }
    assert_equal [
      { "name" => "desktop-guest-amd64", "path" => "images/guest/out/initrd\nimages/guest/out/rootfs.img\nimages/guest/out/vmlinuz\nimages/guest/out/manifest.json\n", "retention-days" => 1, "if-no-files-found" => "error" },
      { "name" => "desktop-agent-disk", "path" => "desktop/dist/agent.img", "retention-days" => 1, "if-no-files-found" => "error" },
      # The measurements' own agent disk, with the probe that writes the kernel's log, never the app's.
      { "name" => "desktop-agent-measure-disk", "path" => "desktop/test/vm/win32/measure/probing/agent.img", "retention-days" => 1, "if-no-files-found" => "error" },
    ], kept.fetch("vm-guest")
    boot = jobs.fetch("vm-boot")
    assert_equal true, boot.fetch("continue-on-error")
    assert_equal "vm-guest", boot.fetch("needs")
    assert_equal %w[desktop-win-vm-boot], kept.fetch("vm-boot").map { |with| with.fetch("name") }
    assert_equal({ "vm-guest" => 3, "vm-boot" => 1 }, kept.reject { |_, withs| withs.empty? }.transform_values(&:length))
    # Taken from this run alone, by no token, before the measurement that boots them.
    steps = boot.fetch("steps")
    taken = steps.select { |step| step["uses"].to_s.start_with?("actions/download-artifact@") }
    assert_equal [{ "name" => "desktop-guest-amd64", "path" => "images/guest/out" }, { "name" => "desktop-agent-disk", "path" => "desktop/dist" }, { "name" => "desktop-agent-measure-disk", "path" => "desktop/test/vm/win32/measure/probing" }], taken.map { |step| step.fetch("with") }
    measured = steps.index { |step| step["run"].to_s.include?("npx vitest run -c test/vm/win32/measure/vitest.config.ts requirements boot") }
    refute_nil measured
    taken.each { |step| assert_operator steps.index(step), :<, measured }
    # The app's agent disk where the development service lays it out, as the installed app has it, never named apart.
    refute steps[measured].fetch("env").key?("SUROGATE_VM_AGENT_DISK")
    installed = steps.index { |step| step["run"].to_s.strip == 'powershell -ExecutionPolicy Bypass -File dev-service.ps1 -AgentDisk ..\..\dist\agent.img' }
    refute_nil installed
    assert_operator installed, :<, measured
    # Booted as Hyper-V opens a disk, a fixed VHD: the agent disk given its footer in place by the app's own code, once
    # it is taken and the code is built, and before the boot.
    footer = steps.index { |step| step["run"].to_s.strip == "node scripts/vhd-footer.mjs dist/agent.img" }
    refute_nil footer
    assert_equal "desktop", steps[footer].fetch("working-directory")
    assert_operator steps.index(taken.last), :<, footer
    assert_operator steps.index { |step| step["name"] == "Build the app's code" }, :<, footer
    assert_operator footer, :<, installed
    jobs.each do |name, job|
      next if name == "vm-boot"

      refute(job.fetch("steps").any? { |step| step["uses"].to_s.start_with?("actions/download-artifact@") }, "#{name} takes an artifact")
    end
    jobs.each_value do |job|
      job.fetch("steps").each do |step|
        refute step.fetch("with", {}).key?("overwrite"), "#{step["name"]} overwrites an artifact"
        refute_match(/upload-artifact|download-artifact|gh run download|actions\/artifacts/, step["run"].to_s, "#{step["name"]} keeps or takes an artifact by hand")
      end
    end
  end

  def test_the_arm64_guest_is_built_and_tested_by_hand_on_an_arm64_runner_and_holds_nothing_to_leak
    arm64 = YAML.load_file(ARM64)
    on = triggers(arm64)
    # Until the release ships the macOS app, arm64's guest is built only when someone asks, on the ref
    # they name: never by a push or a pull request, whose code it would build and boot.
    assert_equal %w[workflow_dispatch], on.keys
    inputs = on.fetch("workflow_dispatch").fetch("inputs")
    assert_equal %w[ref], inputs.keys
    assert_equal [true, "string"], inputs.fetch("ref").values_at("required", "type")
    assert_equal ["jobs", "name", "permissions", "true"], arm64.keys.map(&:to_s).sort
    assert_equal({ "contents" => "read" }, arm64.fetch("permissions"))
    jobs = arm64.fetch("jobs")
    assert_equal %w[guest], jobs.keys
    job = jobs.fetch("guest")
    # Natively, on an arm64 runner: Docker runs the build's programs as the arch's own.
    assert_equal "blacksmith-4vcpu-ubuntu-2404-arm", job.fetch("runs-on")
    # Bounded early, once it has started: a cold build of the guest, and the VM suite.
    assert_operator job.fetch("timeout-minutes"), :<=, 90
    # It publishes nothing: no secret in any form, no Environment, no permission of its own, and
    # nothing sent to R2 or attached to a release. It keeps one artifact alone, a week with the run:
    # the packed image and its manifest, for a Mac's first boot of the guest by hand.
    refute_match(/secrets/i, arm64.to_s)
    refute job.key?("environment")
    refute job.key?("permissions")
    refute_match(/publish\.sh|\bR2_|\bS3_|\baws\b|gh release|download-artifact/, arm64.to_s)
    steps = job.fetch("steps")
    kept = steps.select { |step| step["uses"].to_s.start_with?("actions/upload-artifact@") }
    assert_equal 1, kept.length
    assert_equal({ "name" => "desktop-guest-arm64", "path" => "images/guest/out/*.zst\nimages/guest/out/manifest.json\n", "retention-days" => 7, "if-no-files-found" => "error" }, kept.first.fetch("with"))
    steps.each { |step| refute_match(/upload-artifact|actions\/artifacts/, step["run"].to_s) }
    assert_equal({ "ref" => "${{ inputs.ref }}", "persist-credentials" => false }, steps.first.fetch("with"))
    runs = steps.map { |step| step["run"].to_s }
    build = runs.index { |run| run.include?("images/guest/build.sh --arch arm64 images/guest/out") }
    image = runs.index { |run| run.include?("vitest run test/guest-image.test.ts") }
    kvm = steps.index { |step| step["id"] == "kvm" }
    suite = runs.index { |run| run.include?("vitest run test/vm") }
    refute_nil build
    refute_nil image
    refute_nil kvm
    assert_operator build, :<, suite
    assert_operator kvm, :<, suite
    # The VM suite boots the arm64 image only where the runner has KVM, and says when it has none.
    assert_includes runs[kvm], "[ -c /dev/kvm ]"
    assert_includes runs[kvm], "::notice"
    assert_equal "steps.kvm.outputs.present == 'true'", steps[suite].fetch("if")
    refute steps[suite].fetch("env", {}).key?("SUROGATE_VM_KVM"), "the suite runs emulated"
    assert_equal "1", steps[suite].fetch("env").fetch("SUROGATE_VM_TESTS")
  end

  def test_the_workflow_has_no_key_of_its_own_but_its_name_its_trigger_its_permissions_and_its_jobs
    # A shell, an environment or a concurrency of the workflow's own is every job's: a shell that
    # traces would print what a step reads, an env would be every step's, the release key's step
    # among them, and a concurrency that cancels would cut a send short.
    assert_equal ["jobs", "name", "permissions", "true"], @workflow.keys.map(&:to_s).sort
    assert_equal({ "contents" => "write", "packages" => "write" }, @workflow.fetch("permissions"))
  end

  # Each workflow file of the repository, read: a second file is started by its own trigger, and
  # reads the repository's secrets as this one does.
  def workflows
    Dir[".github/workflows/*"].sort.to_h { |file| [File.basename(file), YAML.load_file(file)] }
  end

  def test_every_secret_is_read_by_its_own_name_in_its_own_step_in_every_workflow_file
    r2 = %w[R2_ENDPOINT R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY]
    # Who reads which secret: each file, each job, each step. R2's keys can rewrite
    # desktop/install.sh, which runs as root on each new install; the desktop's release key signs
    # what every installed app takes. A reader that is not here is one too many.
    allowed = {
      "desktop-guest-arm64.yml" => {},
      "desktop-macos.yml" => {},
      "desktop-windows.yml" => {},
      "release.yml" => {
        ["images", "Log in to GHCR"] => %w[GITHUB_TOKEN],
        ["npm", "Publish SDK packages"] => %w[NPM_TOKEN],
        ["desktop-vm-image", "Look for the image's key in our releases, and check R2 against it"] => r2,
        ["desktop-vm-image", "Publish the image"] => r2,
        ["desktop-publish", "Sign its manifest"] => %w[DESKTOP_RELEASE_KEY],
        ["desktop-publish", "Publish the release"] => r2,
        ["desktop-win-publish", "Sign its manifest"] => %w[DESKTOP_RELEASE_KEY],
        ["desktop-win-publish", "Publish the release"] => r2,
        # Each rollout hands the cluster's three to the workflow it calls, each by its name.
        ["rollout", nil] => %w[NODE_HOSTS MASTER_HOST SSH_KEY],
        ["rollout-with-windows", nil] => %w[NODE_HOSTS MASTER_HOST SSH_KEY],
      },
      "update-images.yml" => {
        ["update-agent-images", nil] => %w[NODE_HOSTS MASTER_HOST],
        ["update-agent-images", "Write SSH identity file"] => %w[SSH_KEY],
      },
    }
    read = workflows.to_h do |file, workflow|
      # Named with whatever letters, large or small: GitHub reads both the word and the name so.
      named = ->(part) { part.to_s.scan(/secrets\s*\.\s*([A-Za-z0-9_]+)/i).flatten }
      found = {}
      found[[nil, nil]] = named.call(workflow.reject { |key, _| key == "jobs" })
      workflow.fetch("jobs").each do |name, job|
        found[[name, nil]] = named.call(job.reject { |key, _| key == "steps" })
        job.fetch("steps", []).each { |step| (found[[name, step["name"] || step["uses"]]] ||= []).concat(named.call(step)) }
      end
      [file, found.reject { |_, names| names.empty? }]
    end
    assert_equal allowed, read
    # And by no other way than its name: not by brackets, not all of them at once, and not handed
    # on whole to a workflow that is called. Every use of the word, outside a comment, is the word,
    # a dot and a name; or it is the key of a list of names, each on a line of its own, as the
    # release's rollout hands the cluster's three to the workflow it calls and that workflow
    # declares them: never the key of one word, which would hand on every secret there is.
    Dir[".github/workflows/*"].sort.each do |file|
      text = File.read(file).gsub(/^\s*#.*$/, "")
      lists = text.scan(/^\s*secrets:\s*$/).length
      assert_equal text.scan(/secrets/i).length, text.scan(/secrets\.[A-Za-z0-9_]+/).length + lists, "#{file} reads a secret by another way than its name"
      refute_match(/\binherit\b/, text, file)
    end
  end

  # *value* as one word: its keys in order of their names, whatever order the file has them in.
  def held(value)
    ordered = lambda do |part|
      case part
      when Hash then part.keys.map(&:to_s).sort.map { |key| [key, ordered.call(part.find { |name, _| name.to_s == key }[1])] }
      when Array then part.map { |each| ordered.call(each) }
      else part
      end
    end
    Digest::SHA256.hexdigest(JSON.generate(ordered.call(value)))[0, 16]
  end

  def test_every_job_is_the_one_this_test_has_read_step_for_step_in_every_workflow_file
    # A step needs no secret's name to send one away. In a job that holds one, a step before the
    # one that is handed it can add a line to the script that step runs, the step's own lines can
    # post what they were handed, a later step can send a file an earlier one wrote, and a
    # container, another runner or a shell that traces has every step's. Every job here holds a
    # secret, a token that may write, or makes the bytes that are signed or published. So each is
    # held whole: its own keys (its runner, its needs, its permissions, its environment, its
    # env, its container, its shell) as one word, and each of its steps as one, in order. A change
    # to any of them fails here, and says the word to put in its place once the change is read:
    # that reading is what this test asks for, of whoever changes a job that can leak.
    read = {
      "desktop-guest-arm64.yml" => {
        "guest" => ["eca5e065813cef05", %w[4a90b485a7208e36 d2e7366771aefc4f ddc9ff3ebe1d8351 bd6e6df4eb2ca928 8c986439d5d86488 9443e3fdb92fa905 6301448be0b1760c 53a6f85d9c3376f6 8a9f74820350edf8 d20f5f9b98bf4212]],
      },
      "desktop-macos.yml" => {
        "unit" => ["d5e8d2ae7f990c0b", %w[4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 926d4e956f967fed 144d905c43662944 a2447334158f4627]],
        "file-host" => ["6e1d22587a09112c", %w[4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 af11daf312d88a9c cef9c04da9316c5d b6677173b7a766d9 65c59a548203f2a3 4aaeb9cc7e06d889]],
        "vm-native" => ["d5e8d2ae7f990c0b", %w[4a90b485a7208e36 5f09c6834194b464 52c04af5d542ff2d aa9be0ef548ec62e 196c921f1237fc02 fb5acba9341bb560]],
        "shell" => ["6e1d22587a09112c", %w[4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 926d4e956f967fed d57a229d162b158b 82ea49c2e002c4eb bb26d389f9d8b55f 2734cf3c0643d000 512eb34e3e8c1e1d bea3be8dc0d7d250]],
        "app" => ["ae8e071452550d33", %w[4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 5b86e1c519cf457a 021891c04ae48470 ddaf78ce5b4faa4c c5f950b2471bf664 2211ef10baa6e3dc 169fd889d47c83b7 4da2b501c8e59078 4e6849a8a58355bd]],
      },
      "desktop-windows.yml" => {
        "unit" => ["943604becd7dec07", %w[fff07e32632a765a 4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 246ce4a71967366d db6273944f5b635b 3a48c3294046bd31 a2a1334e455b966d]],
        "file-host" => ["bf7e4c284f3ad2df", %w[fff07e32632a765a 4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 987851a1dbf1a172 425e4aed22ec6aa0 ecfd01e865dc13c5 de490559b3a5e7c3 db0e0af78d804912 1204467084650a5f df980f3cfa08ab88]],
        "vm-probe" => ["0f4d9654808221f2", %w[8e0c7ce31c19bfe2 4a90b485a7208e36 3509e4e1565f84e3 bc70f17873d318df]],
        "vm-native" => ["2e44d386fc90b194", %w[fff07e32632a765a 4a90b485a7208e36 ab9ce27f59b58a83 6241cbfd200cc438]],
        "vm-backend" => ["943604becd7dec07", %w[fff07e32632a765a 4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 3a48c3294046bd31 e222861c9a8894a5]],
        "vm-guest" => ["ab2559772a48cb7e", %w[4a90b485a7208e36 d2e7366771aefc4f ddc9ff3ebe1d8351 0671721aededeb3b 18d2126a5450e6bd 06af0ce2143d3c1f 8c986439d5d86488 a6aeb1f05887979b 09d5e59cd91883c6 e65248e95c654f75 35697d500667a260 7553a543a3fb9bdd]],
        "vm-boot" => ["34f7796e82983970", %w[fff07e32632a765a 4a90b485a7208e36 d2e7366771aefc4f 0e63e61abc7fc19f ddc9ff3ebe1d8351 0686e10d83c7fcc6 535e74b2af1d7815 ee88c8a9aa9b67f9 83c3999eb9a772b5 7e59534427c2acc1 72442731f8075eaa 6c5f0ebc85fc17d9 85c6bbf8d6ef4ca0 1edc8d7d3216b9d8]],
        "msi" => ["825bfae6263325c0", %w[fff07e32632a765a 4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 246ce4a71967366d 425e4aed22ec6aa0 d9093d282ffd91e6 7121de72b936c1d1 a67b86708c1ed4b3 cd146ae4278c5a3a b56f5703076c66e6 3e923a5e2780b700 1a145085cbf2e2fd 0456457ddf312d12]],
        "shell" => ["bf7e4c284f3ad2df", %w[fff07e32632a765a 4a90b485a7208e36 d2e7366771aefc4f 32e802ba6b7c55d8 246ce4a71967366d 9e6fa2afc24938a3 a3172b7f07aad5c1 7dfd80c81a4c4e52 a7883cf5bcfc7d86 65a1d50c892788ed b0a9f1fb55ecc76d b6744625d7d1a184]],
      },
      "release.yml" => {
        "wheel" => ["4cd0d0e26258996d", %w[dc15019acb8b0420 3f2828c5b55668e8 030b48177ef387aa c1168eec9dd8980f d127b8914f9e2775 89676bc6c688a2df]],
        "images" => ["7e679b02fe3678a5", %w[87fcdec176307d16 0619e48968c28792 617ad6a92e3fd6a0 73c45f0486d37ee1 24a2521493be90fe d971c94aab8fd2bb]],
        "npm" => ["3bb6071d27310db0", %w[87fcdec176307d16 def0caf4b753eb6e 8f8a53d795b99dc8 807dcbfaa4796d62 55af956a9b746fbb 5408cbb4d7c827a5]],
        "desktop-vm-image" => ["891e383153375f2d", %w[87fcdec176307d16 4c034b1990bdc512 c7898d9ce9c764f9 04f76870922f0e0c b7eef4a1402a3c44 84bb50f2712c5cd6 b71b78b7115f448e]],
        "desktop-vm-manifest" => ["f9c7e07cd29827c7", %w[e335da8090635b93 ba3ff68edbc5f647]],
        "desktop-build" => ["e8efdf02c88621d5", %w[87fcdec176307d16 61e8fae8194d2d45 4dc19da678357174 96ec46bd03bed893 18d2126a5450e6bd d8eb498da277e6b9 cc9cc27902be0c44 75ced9156be50298 b76e38f5fbfb4713]],
        "desktop-describe" => ["dcfde8aef5675336", %w[87fcdec176307d16 7f42edae9383c098 70dd0b5fb0038c24]],
        "desktop-signing-mode" => ["058333d3d8a3f05f", %w[bc812f6801c0327b]],
        "desktop-win-build" => ["96928565e2086ccf", %w[fff07e32632a765a 2cbb6c778ee9cf1c d2e7366771aefc4f 32e802ba6b7c55d8 246ce4a71967366d 425e4aed22ec6aa0 96ec46bd03bed893 2b49b547691c5152 3c1d40e7182b4f9e 0dbbd3120ac54992]],
        "desktop-win-sign" => ["5331c2e7e01683ed", %w[8e0c7ce31c19bfe2 2cbb6c778ee9cf1c 657c581b6080781d 8e179919d8dba5d0 8956b5db4a5d9233 d45c1cf5db225c25 5e02861b5f4f1723 ef9eb69984b75079 8d39f0600fb0a5c9 14d765c09ebc5439]],
        "desktop-win-msi" => ["340222aa30f1a523", %w[8e0c7ce31c19bfe2 2cbb6c778ee9cf1c f3dc7563940f8a17 01d5449a1b1abf81 bcbbe8f0cd626b61 481012fa557cec29]],
        "desktop-win-sign-msi" => ["03bec184cbedddaa", %w[8e0c7ce31c19bfe2 2cbb6c778ee9cf1c a0051ea6afa86484 c8a2f46e9dd8850f 2393d71fab9f2fc9 8956b5db4a5d9233 87dc6abcdae90aa5 23a5585e4c8281f3 4c351d6393cf4dc0 59bd8c5a75ce6cdc 8d39f0600fb0a5c9 f87f5ad2f490be6b 49dfba79af0cbedc]],
        "desktop-win-describe" => ["5b0cc765a071d830", %w[87fcdec176307d16 f3dc7563940f8a17 1260aac792094cce]],
        "desktop-publish" => ["9ed0647b31c3b292", %w[87fcdec176307d16 7f42edae9383c098 d864dc15d699f950 f8b9a39c31acaab5]],
        "desktop-win-publish" => ["d55475461df016fa", %w[87fcdec176307d16 3a3b2f3ed7fe05d1 267e964df473dfd2 c60cfc32944bedee]],
        "release" => ["33f64af0592c0ef5", %w[dc15019acb8b0420 b2c42af94398cc27 04917607907da35f d088ed479c39da82 eda05a5d7a75fede]],
        "rollout" => ["c9836d22960a4ba0", %w[]],
        "rollout-with-windows" => ["4a92c695959893a8", %w[]],
      },
      "update-images.yml" => {
        "update-agent-images" => ["bce0913a43e4c4dc", %w[79f419cc70509398 17127a23ac6df9ec ee7d72f941971e8f 8ec0d2118169566d]],
      },
    }
    found = workflows.to_h do |file, workflow|
      [file, workflow.fetch("jobs").to_h { |name, job| [name, [held(job.reject { |key, _| key == "steps" }), job.fetch("steps", []).map { |step| held(step) }]] }]
    end
    found.each do |file, jobs|
      assert_equal read.fetch(file).keys, jobs.keys, "#{file} has other jobs than this test has read"
      jobs.each do |name, (keys, steps)|
        assert_equal read.fetch(file).fetch(name)[0], keys, "#{file}: the keys of #{name} beside its steps are not the ones this test has read (#{keys})"
        names = workflows.fetch(file).fetch("jobs").fetch(name).fetch("steps", []).map { |step| step["name"] || step["uses"] }
        assert_equal read.fetch(file).fetch(name)[1].zip(names), steps.zip(names), "#{file}: a step of #{name} is not the one this test has read"
      end
    end
    assert_equal read.keys, found.keys
    # The build's bytes are the ones that are signed: made on the runner the other jobs trust.
    assert_equal "blacksmith-4vcpu-ubuntu-2404", @workflow.fetch("jobs").fetch("desktop-build").fetch("runs-on")
  end

  def test_each_of_the_desktop_s_environments_is_its_own_jobs_alone_in_every_workflow_file
    # An Environment's secrets reach every job that names it, whatever letters it is named with, and
    # its variables too: the release key's, one job alone; the Windows signing's, the two that sign.
    named = workflows.flat_map do |file, workflow|
      workflow.fetch("jobs").select { |_, job| job.key?("environment") }.map { |name, job| [file, name, job["environment"]] }
    end
    assert_equal [
      ["release.yml", "desktop-win-sign", "desktop-windows-signing"],
      ["release.yml", "desktop-win-sign-msi", "desktop-windows-signing"],
      ["release.yml", "desktop-publish", "desktop-release"],
      ["release.yml", "desktop-win-publish", "desktop-release"],
    ], named
  end

  def test_every_action_is_named_by_its_commit_in_every_workflow_file
    # A tag or a branch can be moved to other code: the build's job writes what is signed, other
    # jobs hold R2's keys beside the actions they run, and the second file's job holds the key to
    # every node of the cluster. So every file is asked, each step of each job and each job that
    # is another workflow, and a file that runs no action today is held to it for the day it does.
    by_commit = %r{\A[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}\z}
    run = Hash.new { |files, file| files[file] = [] }
    workflows.each do |file, workflow|
      run[file]
      workflow.fetch("jobs").each do |name, job|
        named = [job["uses"]] + job.fetch("steps", []).map { |step| step["uses"] }
        named.compact.each do |action|
          # A job that is a workflow of this repository's own, by its path, runs the file of the
          # commit that the run is of: nothing of it can be moved. It is asked like any other file here.
          if action.start_with?("./")
            assert_equal [name, "./.github/workflows/update-images.yml"], [job["uses"] && name, action], "#{file}: #{name} calls a file this test has not read"
            assert File.file?(action), "#{file}: #{name} calls a workflow that is not there"
            next
          end
          assert_match(by_commit, action, "#{file}: #{name} runs an action that is not named by its commit")
          run[file] << action.split("@").first
        end
      end
    end
    assert_equal %w[desktop-guest-arm64.yml desktop-macos.yml desktop-windows.yml release.yml update-images.yml], run.keys
    assert_equal %w[actions/checkout actions/setup-node actions/upload-artifact], run.fetch("desktop-guest-arm64.yml").uniq.sort
    assert_equal %w[actions/checkout actions/setup-node], run.fetch("desktop-macos.yml").uniq.sort
    assert_equal %w[actions/checkout actions/download-artifact actions/setup-node actions/upload-artifact], run.fetch("desktop-windows.yml").uniq.sort
    assert_equal %w[
      actions/checkout actions/download-artifact actions/setup-node actions/upload-artifact astral-sh/setup-uv azure/artifact-signing-action
      azure/login docker/build-push-action docker/login-action docker/setup-buildx-action pnpm/action-setup softprops/action-gh-release
    ], run.fetch("release.yml").uniq.sort
    assert_empty run.fetch("update-images.yml"), "update-images.yml runs an action: name it here, by its commit in the file"
  end

  def test_the_look_for_an_action_reads_the_file_as_the_runner_does
    # Every "uses" of a workflow file's text is one the test above has asked: none is in a place it
    # does not look, as a step of a list that a key of another name holds would be.
    workflows.each do |file, workflow|
      read = workflow.fetch("jobs").sum { |_, job| (job.key?("uses") ? 1 : 0) + job.fetch("steps", []).count { |step| step.key?("uses") } }
      assert_equal File.read(".github/workflows/#{file}").scan(/^\s*(?:-\s+)?uses:/).length, read, "#{file} names an action where no job's steps are"
    end
  end

  def test_each_artifact_is_one_job_s_to_keep_and_is_never_overwritten
    # An artifact is the run's: any job may put a file under its name. The desktop's tarball is the
    # build's alone to keep, and no step may replace what another kept.
    kept = []
    taken = []
    @workflow.fetch("jobs").each do |name, job|
      job.fetch("steps", []).each do |step|
        uses = step["uses"].to_s
        kept << [name, step.fetch("with").fetch("name")] if uses.start_with?("actions/upload-artifact@")
        taken << [name, step.fetch("with").fetch("name")] if uses.start_with?("actions/download-artifact@")
        refute step.fetch("with", {}).key?("overwrite"), "#{name} overwrites an artifact"
        refute_match(/upload-artifact|download-artifact|gh run download|actions\/artifacts/, step["run"].to_s, "#{name} keeps or takes an artifact by hand")
      end
    end
    assert_equal [
      %w[wheel dist], %w[desktop-vm-image desktop-vm-manifest], %w[desktop-build desktop-tarball], %w[desktop-build desktop-agent-disk],
      %w[desktop-win-build desktop-win-unsigned], %w[desktop-win-sign desktop-win-signed], %w[desktop-win-msi desktop-win-msi-unsigned],
      %w[desktop-win-sign-msi desktop-win-msi],
    ], kept
    assert_equal [
      %w[desktop-vm-manifest desktop-vm-manifest], %w[desktop-build desktop-vm-manifest], %w[desktop-describe desktop-tarball],
      %w[desktop-win-build desktop-vm-manifest], %w[desktop-win-build desktop-agent-disk], %w[desktop-win-sign desktop-win-unsigned],
      %w[desktop-win-msi desktop-win-signed], %w[desktop-win-sign-msi desktop-win-msi-unsigned], %w[desktop-win-sign-msi desktop-win-signed],
      %w[desktop-win-describe desktop-win-signed],
      %w[desktop-publish desktop-tarball], %w[desktop-win-publish desktop-win-msi], %w[release dist],
    ], taken
  end
end
