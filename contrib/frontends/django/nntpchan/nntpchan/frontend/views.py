from django.conf import settings
from django.core.urlresolvers import reverse
from django.http import HttpResponse, Http404
from django.shortcuts import render, get_object_or_404
from django.views import generic


from .models import Post, Newsgroup, ModPriv

from captcha.image import ImageCaptcha
from . import util

captcha = ImageCaptcha(fonts=settings.CAPTCHA_FONTS)


class Postable:
    """
    postable view
    checks captcha etc
    """

    def context_for_get(self, request, defaults):
        defaults['captcha'] = reverse('frontend:captcha')
        defaults['refresh_url'] = request.path
        return defaults

    def handle_post(self, request, **kwargs):
        """
        handle post request, implement in subclass
        """
        return None, 'handle_post() not implemented'

    def handle_mod(self, request):
        """
        handle moderation parameters
        """
        if 'modactions' in request.POST:
            actions = request.POST['modactions']
            if len(actions) > 0:
                body = ''
                for line in actions.split('\n'):
                    line = line.strip()
                    if len(line) > 0:
                        body += '{}\n'.format(line)
                key = None
                if 'secret' in request.POST:
                    key = request.POST['secret']
                _, err = util.createPost('ctl', '', {'message': body}, {}, key)
                return True, err
        return False, None
    
    def post(self, request, **kwargs):
        ctx = {
            'error' : 'invalid captcha',
            'only_mod': False
        }
        # bypass captcha for mod users
        mod = None
        if 'mod' in request.session:
            mod = request.session['mod']
        if mod is None:
            solution = request.session['captcha']
            if solution is not None:
                if 'captcha' in request.POST:
                    if request.POST['captcha'].lower() == solution.lower():
                        processed, err = self.handle_mod(request)
                        if processed:
                            ctx['error'] = err or 'report made'
                            ctx['msgid'] = ''
                        else:
                            ctx['msgid'], ctx['error'] = self.handle_post(request, **kwargs)
            request.session['captcha'] = ''
        else:
            # we have a mod session
            ctx['msgid'], ctx['error'] = self.handle_post(request, **kwargs)
            
        request.session.save()
        code = 201
        if ctx['error']:
            code = 200
        elif ctx['msgid']:
            ctx['refresh_url'] = reverse('frontend:thread', args=[util.hashid(ctx['msgid'])])
        return HttpResponse(content=render(request, 'frontend/postresult.html', ctx), status=code)

        
class BoardView(generic.View, Postable):
    template_name = 'frontend/board.html'
    context_object_name = 'threads'
    model = Post

    def handle_post(self, request, name):
        """
        make a new thread
        """
        name = 'overchan.{}'.format(name)
        if not util.newsgroup_valid(name):
            return None, "invalid newsgroup: {}".format(name)
        self.handle_mod(request)
        return util.createPost(name, None, request.POST, request.FILES)

    
    def get(self, request, name, page="0"):
        newsgroup = 'overchan.{}'.format(name)
        try:
            page = int(page or "0")
        except:
            page = 0
        if page < 0:
            page = 0
        try:
            group = Newsgroup.objects.get(name=newsgroup)
        except Newsgroup.DoesNotExist:
            raise Http404("no such board")
        else:
            begin = page * group.posts_per_page
            end = begin + group.posts_per_page - 1
            roots = self.model.objects.filter(newsgroup=group, reference='').order_by('-last_bumped')[begin:end]
            ctx = self.context_for_get(request, {'threads': roots, 'page': page, 'name': newsgroup, 'button': 'new thread'})
            if page < group.max_pages:
                ctx['nextpage'] = reverse('frontend:board', args=[name, page + 1])
            if page == 1:
                ctx['prevpage'] = reverse('frontend:board-front', args=[name])
            if page > 1:
                ctx['prevpage'] = reverse('frontend:board', args=[name, page - 1])
            return render(request, self.template_name, ctx)
        
class ThreadView(generic.View, Postable):
    template_name = 'frontend/thread.html'
    model = Post

    def handle_post(self, request, op):
        """
        make a new thread
        """
        post = get_object_or_404(self.model, posthash=op)
        name = post.newsgroup.name
        if not util.newsgroup_valid(name):
            return None, "invalid newsgroup: {}".format(name)
        return util.createPost(name, post.msgid, request.POST, request.FILES)

    
    def get(self, request, op):
        posts = get_object_or_404(self.model, posthash=op)
        ctx = self.context_for_get(request, {'op': posts, 'button': 'reply'})
        return render(request, self.template_name, ctx)
    
class FrontPageView(generic.View):
    template_name = 'frontend/frontpage.html'
    model = Post

    def get(self, request, truncate=5):
        if truncate <= 0:
            truncate = 5
        posts = self.model.objects.order_by('-posted')[:truncate]
        ctx = {'posts' : posts}
        return render(request, self.template_name, ctx)
    

class ModView(generic.View, Postable):

    def show_login(self, request):
        """
        handle login page
        """
        request.session['mod'] = None
        request.session.save()
        return render(request, 'frontend/modlogin.html')

    def logout(self, request):
        request.session['mod'] = None
        request.session.save()
        return render(request, 'frontend/redirect.html', { 'url' : reverse('frontend:mod')})

    def post(self, request):
        action = None
        if 'action' in request.POST:
            action = request.POST['action']
        mod = None
        msg = 'bad login'
        if 'mod' in request.session:
            mod = request.session['mod']
        if action == 'logout':
            return self.logout(request)
        if action == 'login' and 'secret' in request.POST and mod is None:
            # try login
            sk = request.POST['secret']
            pk = None
            try:
                pk = util.to_public(sk)
                print (pk)
            except:
                msg = 'bad key format'
            if pk is not None and ModPriv.has_access(ModPriv.LOWEST, pk):
                mod = request.session['mod'] = {'sk' : sk, 'pk' : pk}
                request.session.save()
                # login okay
                return render(request, 'frontend/redirect.html', {'url' : reverse('frontend:mod'), 'msg' : 'logged in'})
        if mod is None:
            # no mod session
            return render(request, 'frontend/redirect.html', {'url' : reverse('frontend:mod'), 'msg' : msg } )
        else:
            # do mod action
            return self.handle_mod_action(request, mod, action)

    def handle_mod_action(self, request, mod, action):
        msg = 'no action made'
        if action is not None:
            if action == 'delete':
                # handle bulk delete
                if 'posts' in request.POST:
                    body = '\ndelete '.join(request.POST['posts'].split())
                    sk = mod['sk']
                    _, err = util.createPost('ctl', '', {'message' : body }, {}, sk)
                    if err:
                        msg = 'error: {}'.format(err)
                    else:
                        msg = 'okay'
        
        return render(request, 'frontend/redirect.html', {'url' : reverse('frontend:mod'), 'msg' : msg} )
    
    def get(self, request):
        mod = None
        if 'mod' in request.session:
            mod = request.session['mod']
        if mod:
            return render(request, 'frontend/modpage.html', {'mod' : mod })
        else:
            return self.show_login(request)
        
        
def modlog(request, page=None):
    page = int(page or '0')
    ctx = {
        'page': page,
    }
    if page > 0:
        ctx['prevpage'] = reverse('frontend:modlog-page', args=[page - 1])

    group, _ = Newsgroup.objects.get_or_create(name='ctl')
    if page < group.max_pages:
        ctx['nextpage'] = reverse('frontend:modlog-page', args=[page + 1])
    begin = group.posts_per_page * page
    end = begin + group.posts_per_page - 1
    ctx['threads'] = Post.objects.filter(newsgroup='ctl').order_by('-last_bumped')[begin:end]
    return render(request, 'frontend/board.html', ctx)

def create_captcha(request):
    solution = util.randstr(7).lower()
    request.session['captcha'] = solution
    request.session.save()
    c = captcha.generate(solution)
    r =HttpResponse(c)
    r['Content-Type'] = 'image/png'
    return r

def keygen(request):
    """
    generate new keypair
    """
    ctx = {}
    ctx['sk'], ctx['pk'] = util.keygen()
    return render(request, 'frontend/keygen.html', ctx)
